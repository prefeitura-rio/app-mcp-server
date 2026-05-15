"""
Análise visual pra mídia inbound do WhatsApp (Gemini Vision).

Estende o `register_inbound_media` (audit-only stub) com análise da imagem
de fato via Gemini multimodal. O agente pode chamar AMBAS no mesmo turn:
  1. register_inbound_media → audit + ack
  2. analyze_inbound_image  → análise visual → workflow apropriado

Caminhos de fonte de bytes (em ordem de preferência):
  - `meta_media_id` (canal canônico, ADR-017) → Graph API + Meta CDN
  - `salesforce_download_path` (UWC legacy, ADR-014) → SF REST OAuth
  - `image_bytes_base64` (testes manuais)
  - `local_image_path` (sandbox /tmp, IS_LOCAL=true)

Refator 2026-05-14 noite (ADR-018): bulk da lógica multi-source + magic-byte +
Gemini call extraído pra `src/utils/inbound_media_shared.py`. Este arquivo
fica só com o que é vision-specific: prompt, allowlist, reply builder.
"""

from pathlib import Path
from typing import Any, Dict, Optional

from google import genai

from src.config import env
from src.utils.inbound_media_shared import (
    call_gemini_with_blob,
    deferred_no_bytes,
    deferred_no_gemini_key,
    error_gemini_failed,
    parse_analysis_json,
    rejected_subtype_mismatch,
    resolve_inbound_bytes,
)
from src.utils.log import logger

# Sandbox seguro pra `local_image_path` em testes locais. Caminhos fora
# desse prefixo são rejeitados em produção. Resolvido pra normalizar symlinks
# (`/tmp` → `/private/tmp` no macOS).
_LOCAL_IMAGE_PATH_ALLOWED_PREFIX = Path("/tmp").resolve()


_VISION_MODEL = "gemini-2.5-flash"
_ACCEPTED_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif"}
_MAX_BYTES = 20 * 1024 * 1024  # 20 MB — limite inline_data Gemini

# MIME do Graph API → extension canônica (strip codec/charset params no caller)
_MIME_TO_EXT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}

_ANALYSIS_PROMPT_PT_BR = """\
Você é um classificador de serviços da Prefeitura do Rio de Janeiro.

Um cidadão acabou de enviar essa imagem via WhatsApp pra reportar um
problema de serviço público. Analise a foto e responda em JSON estrito
(sem markdown, sem comentários) com EXATAMENTE este schema:

{
  "descricao": "<descrição objetiva do que a foto mostra, max 200 chars>",
  "problema_detectado": <true|false>,
  "categoria": "<um de: luminaria_publica | poda_arvore | buraco_via | lixo_irregular | iluminacao_publica | sinalizacao | outro | nao_aplica>",
  "detalhes": "<o que especificamente parece estar errado, max 250 chars; vazio se problema_detectado=false>",
  "workflow_sugerido": "<um de: reparo_luminaria | poda_de_arvore | nenhum>",
  "confianca": "<alta|media|baixa>"
}

REGRAS:
- Se a imagem NÃO mostra problema de serviço público (foto de selfie, comida,
  paisagem, animal, screenshot etc.), use problema_detectado=false,
  categoria="nao_aplica", workflow_sugerido="nenhum".
- Se mostra luminária com problema (caída, quebrada, apagada visível de noite,
  bulbo estourado): workflow_sugerido="reparo_luminaria".
- Se mostra árvore com galhos pra cortar, caída ou que ameaça fios:
  workflow_sugerido="poda_de_arvore".
- Pra outros problemas (buraco, lixo, sinalização) sem workflow MCP existente,
  use workflow_sugerido="nenhum" e ainda assim preencha categoria.
- Confiança "alta" se você tem certeza visual; "baixa" se a foto é
  ambígua/escura/cropada.
"""


def _read_image_bytes(local_image_path: Optional[str]) -> Optional[bytes]:
    """Lê os bytes do arquivo local com checks anti-arbitrary-read.

    Restringe a leitura a:
      - somente quando `IS_LOCAL=true` no ambiente, E
      - somente paths dentro de `_LOCAL_IMAGE_PATH_ALLOWED_PREFIX` (resolvido
        contra symlinks).
    Em produção (IS_LOCAL=false), retorna None.
    """
    if not local_image_path:
        return None
    if not env.IS_LOCAL:
        logger.warning(
            "analyze_inbound_image: local_image_path ignorado em produção "
            "(IS_LOCAL=false). Use image_bytes_base64."
        )
        return None
    p = local_image_path
    if p.startswith("file://"):
        p = p[len("file://") :]
    path = Path(p).resolve()
    if not path.is_relative_to(_LOCAL_IMAGE_PATH_ALLOWED_PREFIX):
        logger.warning(
            f"analyze_inbound_image: local_image_path {path!s} fora do "
            f"prefixo permitido {_LOCAL_IMAGE_PATH_ALLOWED_PREFIX!s}; ignorado."
        )
        return None
    if not path.is_file():
        logger.warning(f"analyze_inbound_image: arquivo não encontrado: {path}")
        return None
    size = path.stat().st_size
    if size > _MAX_BYTES:
        logger.warning(
            f"analyze_inbound_image: arquivo {size} bytes > limite {_MAX_BYTES}"
        )
        return None
    return path.read_bytes()


def _mime_from_extension(file_extension: Optional[str]) -> str:
    """Extension canônica → MIME pro Gemini blob (inline_data).

    Default sensato pra extensions não-mapeadas (Gemini é tolerante a
    `image/jpeg` mesmo pra PNG/WebP, então prefere isso a falhar).
    """
    ext = (file_extension or "jpg").lower().lstrip(".")
    if ext == "jpg":
        ext = "jpeg"
    if ext not in {"jpeg", "png", "webp", "gif"}:
        ext = "jpeg"
    return f"image/{ext}"


def _build_reply_from_analysis(analysis: Dict[str, Any]) -> str:
    """Monta resposta amigável pro cidadão baseada na análise visual."""
    if not analysis.get("parsed"):
        return "Recebi sua imagem! Pode me descrever em texto o que precisa registrar?"
    if not analysis.get("problema_detectado"):
        return (
            "Recebi sua imagem! Mas não consegui identificar um problema de "
            "serviço público nela. Pode me descrever em texto o que precisa?"
        )
    workflow = analysis.get("workflow_sugerido", "nenhum")
    descricao = analysis.get("detalhes") or analysis.get("descricao") or ""
    if workflow == "reparo_luminaria":
        return (
            f"Recebi sua foto. Pelo que consegui ver: {descricao}. "
            "Vou te ajudar a abrir um chamado de reparo de luminária — "
            "você confirma que quer prosseguir?"
        )
    if workflow == "poda_de_arvore":
        return (
            f"Recebi sua foto. Pelo que consegui ver: {descricao}. "
            "Vou te ajudar a abrir uma solicitação de poda de árvore — "
            "você confirma que quer prosseguir?"
        )
    return (
        f"Recebi sua foto. Pelo que consegui ver: {descricao}. "
        "Esse tipo de problema ainda não tenho um fluxo automático — "
        "pode me descrever em texto pra eu te encaminhar?"
    )


async def analyze_inbound_image(
    user_number: str,
    file_extension: Optional[str] = None,
    local_image_path: Optional[str] = None,
    image_bytes_base64: Optional[str] = None,
    salesforce_download_path: Optional[str] = None,
    meta_media_id: Optional[str] = None,
    message_id: Optional[str] = None,
    content_version_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Analisa imagem inbound via Gemini Vision e classifica problema reportado.

    Ver docstring do módulo pra fontes de bytes em ordem de preferência. A
    bulk da lógica vive em `src.utils.inbound_media_shared` — este wrapper
    só passa config + chama helpers.
    """
    source = await resolve_inbound_bytes(
        tool_name="analyze_inbound_image",
        meta_media_id=meta_media_id,
        salesforce_download_path=salesforce_download_path,
        content_version_id=content_version_id,
        local_path=local_image_path,
        bytes_base64=image_bytes_base64,
        file_extension=file_extension,
        accepted_extensions=_ACCEPTED_EXTENSIONS,
        mime_to_extension=_MIME_TO_EXT,
        max_bytes=_MAX_BYTES,
        media_domain="image",
        local_path_reader=_read_image_bytes,
    )
    if source.error_response is not None:
        return source.error_response
    image_bytes = source.image_bytes
    file_extension = source.file_extension

    if not image_bytes:
        return deferred_no_bytes("image")

    # Anti-hallucination: magic bytes batem com tipo declarado?
    # Sem isso, Gemini com bytes errados (OGG enviado como image/jpeg via
    # Apex correlation race) pode alucinar análise visual plausível.
    from src.utils.media_sniff import detect_media_subtype, matches_expected_extension

    if not matches_expected_extension(image_bytes, file_extension):
        detected_subtype = detect_media_subtype(image_bytes) or "unknown"
        logger.warning(
            f"analyze_inbound_image: subtype dos magic bytes não bate com a "
            f"extension declarada (detected_subtype={detected_subtype!r}, "
            f"declared file_extension={file_extension!r}, "
            f"first_bytes={image_bytes[:8]!r}, message_id={message_id}, "
            f"content_version_id={content_version_id})"
        )
        return rejected_subtype_mismatch(
            detected=detected_subtype,
            declared=file_extension or "",
            message_id=message_id,
            media_domain="image",
        )

    if not env.GEMINI_API_KEY:
        logger.warning("analyze_inbound_image: GEMINI_API_KEY não configurada")
        return deferred_no_gemini_key()

    client = genai.Client(api_key=env.GEMINI_API_KEY)
    mime = _mime_from_extension(file_extension)
    gemini_result = await call_gemini_with_blob(
        client=client,
        model=_VISION_MODEL,
        prompt_text=_ANALYSIS_PROMPT_PT_BR,
        mime_type=mime,
        blob_bytes=image_bytes,
        tool_name="analyze_inbound_image",
    )
    if gemini_result.text is None:
        return error_gemini_failed(
            gemini_result.error_detail or "Gemini call failed", "image"
        )

    analysis = parse_analysis_json(
        gemini_result.text, tool_name="analyze_inbound_image"
    )
    suggested_reply = _build_reply_from_analysis(analysis)

    logger.info(
        f"analyze_inbound_image: user_number={user_number} "
        f"categoria={analysis.get('categoria')!r} "
        f"workflow={analysis.get('workflow_sugerido')!r} "
        f"confianca={analysis.get('confianca')!r} "
        f"message_id={message_id} content_version_id={content_version_id}"
    )

    return {
        "status": "analyzed",
        "analysis": analysis,
        "suggested_reply_pt_br": suggested_reply,
    }
