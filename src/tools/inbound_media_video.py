"""
Análise de vídeo pra mídia inbound do WhatsApp (Gemini multimodal video).

Espelha o desenho de `inbound_media_vision.py`/`inbound_media_audio.py` (ADR-018):
- Bulk da lógica multi-source (Meta CDN / Salesforce / base64 / local)
  + magic-byte + Gemini call vive em `src/utils/inbound_media_shared.py`.
- Este módulo fica só com o que é video-specific: modelo, allowlist de
  extensions/MIMEs, prompt PT-BR, helper de reply.

Caminhos de fonte de bytes (em ordem de preferência):
  - `meta_media_id` (canal canônico, ADR-017) → Graph API + Meta CDN
  - `salesforce_download_path` (UWC legacy, ADR-014) → SF REST OAuth
  - `video_bytes_base64` (testes manuais; pouco confiável >~10KB)
  - `local_video_path` (sandbox /tmp, IS_LOCAL=true)

Gemini multimodal limita inline_data a 20MB — WhatsApp limita upload de
vídeo a 16MB, então inline sempre cabe. Pra vídeos maiores (futuro,
Files API), criar helper análogo a `call_gemini_with_blob` que faz
upload separado e referencia por URI.
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

# Sandbox seguro pra `local_video_path` em testes locais. Caminhos fora
# desse prefixo são rejeitados em produção. Resolvido pra normalizar symlinks
# (`/tmp` → `/private/tmp` no macOS).
_LOCAL_VIDEO_PATH_ALLOWED_PREFIX = Path("/tmp").resolve()


_VIDEO_MODEL = "gemini-2.5-flash"
_ACCEPTED_EXTENSIONS = {"mp4", "m4v", "mov", "3gp", "3gpp", "webm"}
_MAX_BYTES = 20 * 1024 * 1024  # 20 MB — limite inline_data Gemini

# MIME do Graph API → extension canônica
_MIME_TO_EXT = {
    "video/mp4": "mp4",
    "video/x-m4v": "m4v",
    "video/quicktime": "mov",
    "video/3gpp": "3gp",
    "video/3gpp2": "3gp",
    "video/webm": "webm",
}

_ANALYSIS_PROMPT_PT_BR = """\
Você é um classificador de serviços da Prefeitura do Rio de Janeiro.

Um cidadão acabou de enviar esse vídeo via WhatsApp pra reportar um
problema de serviço público. Analise o vídeo (frames + áudio se houver)
e responda em JSON estrito (sem markdown, sem comentários) com EXATAMENTE
este schema:

{
  "descricao": "<descrição objetiva do que o vídeo mostra, max 250 chars>",
  "problema_detectado": <true|false>,
  "categoria": "<um de: luminaria_publica | poda_arvore | buraco_via | lixo_irregular | iluminacao_publica | sinalizacao | enchente_alagamento | outro | nao_aplica>",
  "detalhes": "<resumo objetivo e conciso do problema em 3ª pessoa, max 100 chars; vazio se problema_detectado=false>",
  "transcricao_audio": "<se o cidadão fala no vídeo, transcreva fielmente em PT-BR, max 500 chars; vazio se sem áudio falado>",
  "resumo_audio": "<se há áudio falado, resumo objetivo em 3ª pessoa, max 100 chars; vazio se sem áudio>",
  "workflow_sugerido": "<um de: reparo_luminaria | poda_de_arvore | nenhum>",
  "confianca": "<alta|media|baixa>"
}

REGRAS:
- Se o vídeo NÃO mostra problema de serviço público (selfie, animal,
  evento social, comida etc.), use problema_detectado=false,
  categoria="nao_aplica", workflow_sugerido="nenhum".
- Se mostra luminária com problema (caída, quebrada, apagada à noite,
  piscando): workflow_sugerido="reparo_luminaria".
- Se mostra árvore com galhos pra cortar, caída ou ameaçando fios:
  workflow_sugerido="poda_de_arvore".
- Pra outros problemas (buraco, lixo, sinalização, enchente) sem
  workflow MCP existente, use workflow_sugerido="nenhum" e ainda assim
  preencha categoria.
- DETALHES: escreva em 3ª pessoa, objetivo e curto.
- RESUMO_AUDIO: se o cidadão fala no vídeo, resuma em 3ª pessoa o que foi dito.
- Vídeos curtos (<3s) ou com baixa qualidade visual: confiança "baixa"
  e descreva o que conseguiu identificar.
- Se há áudio falado, transcreva E resuma. Se for ruído/música/silêncio, deixe
  transcricao_audio e resumo_audio vazios.
"""


def _read_video_bytes(local_video_path: Optional[str]) -> Optional[bytes]:
    """Lê os bytes do arquivo local com checks anti-arbitrary-read.

    Mesmo padrão de `inbound_media_vision._read_image_bytes`:
      - somente em ambiente local (IS_LOCAL=true)
      - somente paths dentro de `_LOCAL_VIDEO_PATH_ALLOWED_PREFIX`
    Em produção, retorna None.
    """
    if not local_video_path:
        return None
    if not env.IS_LOCAL:
        logger.warning(
            "analyze_inbound_video: local_video_path ignorado em produção "
            "(IS_LOCAL=false). Use video_bytes_base64 ou meta_media_id."
        )
        return None
    p = local_video_path
    if p.startswith("file://"):
        p = p[len("file://") :]
    path = Path(p).resolve()
    if not path.is_relative_to(_LOCAL_VIDEO_PATH_ALLOWED_PREFIX):
        logger.warning(
            f"analyze_inbound_video: local_video_path {path!s} fora do "
            f"prefixo permitido {_LOCAL_VIDEO_PATH_ALLOWED_PREFIX!s}; ignorado."
        )
        return None
    if not path.is_file():
        logger.warning(f"analyze_inbound_video: arquivo não encontrado: {path}")
        return None
    size = path.stat().st_size
    if size > _MAX_BYTES:
        logger.warning(
            f"analyze_inbound_video: arquivo {size} bytes > limite {_MAX_BYTES} "
            f"(usar Files API pra vídeos maiores; não implementado ainda)."
        )
        return None
    return path.read_bytes()


def _mime_from_extension(file_extension: Optional[str]) -> str:
    """Extension canônica → MIME pro Gemini blob (inline_data).

    Default `video/mp4` (formato WhatsApp default) pra extensions não-mapeadas.
    """
    ext = (file_extension or "mp4").lower().lstrip(".")
    if ext == "3gpp":
        ext = "3gp"
    if ext == "m4v":
        # M4V é container MP4 com extension diferente; Gemini não aceita
        # `video/x-m4v` explícito — enviar como mp4. Magic bytes batem.
        ext = "mp4"
    mime_map = {
        "mp4": "video/mp4",
        "mov": "video/quicktime",
        "3gp": "video/3gpp",
        "webm": "video/webm",
    }
    return mime_map.get(ext, "video/mp4")


def _build_reply_from_analysis(analysis: Dict[str, Any]) -> str:
    """Monta resposta amigável pro cidadão baseada na análise do vídeo."""
    if not analysis.get("parsed"):
        return "Recebi seu vídeo! Pode me descrever em texto o que precisa registrar?"
    if not analysis.get("problema_detectado"):
        return (
            "Recebi seu vídeo! Mas não consegui identificar um problema de "
            "serviço público nele. Pode me descrever em texto o que precisa?"
        )
    workflow = analysis.get("workflow_sugerido", "nenhum")
    detalhes = (analysis.get("detalhes") or "").strip()
    resumo_audio = (analysis.get("resumo_audio") or "").strip()

    # Monta o entendimento combinando vídeo + áudio se houver
    partes_entendimento = []
    if detalhes:
        partes_entendimento.append(detalhes.lower())
    if resumo_audio:
        partes_entendimento.append(resumo_audio.lower())

    entendimento = ""
    if partes_entendimento:
        entendimento = f"Entendi que {' e '.join(partes_entendimento)}"

    if workflow == "reparo_luminaria":
        if entendimento:
            return (
                f"{entendimento}. Você deseja abrir um chamado de reparo de luminária?"
            )
        return "Você deseja abrir um chamado de reparo de luminária?"

    if workflow == "poda_de_arvore":
        if entendimento:
            return (
                f"{entendimento}. Você deseja abrir uma solicitação de poda de árvore?"
            )
        return "Você deseja abrir uma solicitação de poda de árvore?"

    # workflow_sugerido='nenhum' mas problema_detectado=true: temos
    # descricao + (eventual) resumo_audio. Continuar atendimento
    # sem pedir pro cidadao repetir info já extraida (Codex P2 2026-05-15).
    if entendimento:
        return (
            f"{entendimento}. Para esse tipo de problema ainda não tenho um fluxo "
            "automatizado — posso te encaminhar pra um atendente humano ou "
            "continuar te ajudando por aqui?"
        )
    return (
        "Recebi seu vídeo. Para esse tipo de problema ainda não tenho um fluxo "
        "automatizado — posso te encaminhar pra um atendente humano ou "
        "continuar te ajudando por aqui?"
    )


async def analyze_inbound_video(
    user_number: str,
    file_extension: Optional[str] = None,
    local_video_path: Optional[str] = None,
    video_bytes_base64: Optional[str] = None,
    salesforce_download_path: Optional[str] = None,
    meta_media_id: Optional[str] = None,
    message_id: Optional[str] = None,
    content_version_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Analisa vídeo inbound via Gemini multimodal e classifica problema reportado.

    Ver docstring do módulo pra fontes de bytes em ordem de preferência. A
    bulk da lógica vive em `src.utils.inbound_media_shared` — este wrapper
    só passa config + chama helpers.
    """
    source = await resolve_inbound_bytes(
        tool_name="analyze_inbound_video",
        meta_media_id=meta_media_id,
        salesforce_download_path=salesforce_download_path,
        content_version_id=content_version_id,
        local_path=local_video_path,
        bytes_base64=video_bytes_base64,
        file_extension=file_extension,
        accepted_extensions=_ACCEPTED_EXTENSIONS,
        mime_to_extension=_MIME_TO_EXT,
        max_bytes=_MAX_BYTES,
        media_domain="video",
        local_path_reader=_read_video_bytes,
    )
    if source.error_response is not None:
        return source.error_response
    video_bytes = source.image_bytes  # campo do shared dataclass — bytes genéricos
    file_extension = source.file_extension

    if not video_bytes:
        return deferred_no_bytes("video")

    # Anti-hallucination: magic bytes batem com tipo declarado?
    from src.utils.media_sniff import detect_media_subtype, matches_expected_extension

    if not matches_expected_extension(video_bytes, file_extension):
        detected_subtype = detect_media_subtype(video_bytes) or "unknown"
        logger.warning(
            f"analyze_inbound_video: subtype dos magic bytes não bate com a "
            f"extension declarada (detected_subtype={detected_subtype!r}, "
            f"declared file_extension={file_extension!r}, "
            f"first_bytes={video_bytes[:12]!r}, message_id={message_id}, "
            f"content_version_id={content_version_id})"
        )
        return rejected_subtype_mismatch(
            detected=detected_subtype,
            declared=file_extension or "",
            message_id=message_id,
            media_domain="video",
        )

    if not env.GEMINI_API_KEY:
        logger.warning("analyze_inbound_video: GEMINI_API_KEY não configurada")
        return deferred_no_gemini_key()

    client = genai.Client(api_key=env.GEMINI_API_KEY)
    mime = _mime_from_extension(file_extension)
    gemini_result = await call_gemini_with_blob(
        client=client,
        model=_VIDEO_MODEL,
        prompt_text=_ANALYSIS_PROMPT_PT_BR,
        mime_type=mime,
        blob_bytes=video_bytes,
        tool_name="analyze_inbound_video",
    )
    if gemini_result.text is None:
        return error_gemini_failed(
            gemini_result.error_detail or "Gemini call failed", "video"
        )

    analysis = parse_analysis_json(
        gemini_result.text, tool_name="analyze_inbound_video"
    )
    suggested_reply = _build_reply_from_analysis(analysis)

    logger.info(
        f"analyze_inbound_video: user_number={user_number} "
        f"categoria={analysis.get('categoria')!r} "
        f"workflow={analysis.get('workflow_sugerido')!r} "
        f"confianca={analysis.get('confianca')!r} "
        f"transcricao_len={len(analysis.get('transcricao_audio') or '')} "
        f"message_id={message_id} content_version_id={content_version_id}"
    )

    return {
        "status": "analyzed",
        "analysis": analysis,
        "suggested_reply_pt_br": suggested_reply,
    }
