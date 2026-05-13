"""
Protótipo de análise visual pra mídia inbound do WhatsApp (Gemini Vision).

Estende o `register_inbound_media` (audit-only stub) com análise da imagem
de fato via Gemini multimodal. O agente pode chamar AMBAS no mesmo turn:
  1. register_inbound_media → audit + ack
  2. analyze_inbound_image  → análise visual → workflow apropriado

Em produção a imagem chega via `salesforce_download_path` (REST do SF). Aqui
no protótipo a tool aceita também `local_image_path` (file:// ou caminho
absoluto) pra permitir teste local sem credencial Salesforce.

Quando o salesforce_download_path é usado, esta tool delega ao chamador
(Engine) o download e injeta os bytes via `image_bytes_base64` — protocolo
proposto pra fase 2 quando o Engine team incluir essa etapa upstream.
"""

import base64
from pathlib import Path
from typing import Any, Dict, Optional

from google import genai
from google.genai import types

from src.config import env
from src.utils.log import logger

# Diretório seguro pra `local_image_path` (sandbox de testes locais). Caminhos
# fora desse prefixo são rejeitados em produção pra evitar arbitrary file read
# via prompt injection no MCP tool. Ver _read_image_bytes. Resolvido (.resolve)
# pra normalizar symlinks tipo `/tmp` → `/private/tmp` no macOS.
_LOCAL_IMAGE_PATH_ALLOWED_PREFIX = Path("/tmp").resolve()


_VISION_MODEL = "gemini-2.5-flash"
_ACCEPTED_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif"}
_MAX_BYTES = 20 * 1024 * 1024  # 20 MB — limite do inline_data do Gemini

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
    """Lê os bytes do arquivo local (sem fazer download de SF).

    Como esta tool é exposta via MCP, qualquer caller (incluindo prompt
    injection) pode passar um path arbitrário. Restringimos a leitura a:
      - somente quando `IS_LOCAL=true` no ambiente, E
      - somente paths dentro de `_LOCAL_IMAGE_PATH_ALLOWED_PREFIX` (resolvido
        contra symlinks).
    Em produção (IS_LOCAL=false), use `image_bytes_base64` injetado pelo
    Engine após o fetch do Salesforce. Ver inbound_media_vision.py docstring.
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


def _path_matches_content_version_id(path: str, content_version_id: str) -> bool:
    """Cross-check: extrai o Id embarcado no salesforce_download_path e
    confirma que bate com o content_version_id do marker.

    Salesforce ContentVersion Ids têm forma 15- ou 18-char alfanuméricos.
    O 15-char é prefix do 18-char (sufixo 3-char é checksum case-sensitive).
    Por isso comparamos só os primeiros 15 chars de cada — independente de
    qual lado é 15 ou 18.
    """
    if not path or not content_version_id:
        return False
    try:
        # Path: .../sobjects/ContentVersion/<Id>/VersionData
        parts = path.rstrip("/").split("/")
        if "ContentVersion" not in parts:
            return False
        idx = parts.index("ContentVersion")
        if idx + 1 >= len(parts):
            return False
        path_id = parts[idx + 1]
    except (ValueError, IndexError):
        return False
    return path_id[:15] == content_version_id[:15]


def _mime_from_extension(file_extension: Optional[str]) -> str:
    ext = (file_extension or "jpg").lower().lstrip(".")
    if ext == "jpg":
        ext = "jpeg"
    if ext not in {"jpeg", "png", "webp", "gif"}:
        ext = "jpeg"  # default razoável
    return f"image/{ext}"


def _parse_analysis(text: str) -> Dict[str, Any]:
    """Tenta extrair JSON da resposta do Gemini, com fallback se vier prosa."""
    import json
    import re

    if not text:
        return {"raw": text, "parsed": False}
    cleaned = text.strip()
    # remove fences ```json ... ```
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return {**parsed, "parsed": True}
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, dict):
                    return {**parsed, "parsed": True}
            except json.JSONDecodeError:
                pass
    logger.warning(f"analyze_inbound_image: JSON inválido do Gemini: {text[:200]}")
    return {"raw": text, "parsed": False}


async def analyze_inbound_image(
    user_number: str,
    file_extension: str,
    local_image_path: Optional[str] = None,
    image_bytes_base64: Optional[str] = None,
    salesforce_download_path: Optional[str] = None,
    message_id: Optional[str] = None,
    content_version_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Analisa visualmente uma imagem inbound via Gemini Vision e classifica
    o problema reportado pelo cidadão.

    Args:
        user_number: telefone E.164 sem '+'.
        file_extension: 'jpg' | 'png' | 'webp' | 'gif'.
        local_image_path: caminho local pro arquivo (file:// ou absoluto).
            Usado em testes locais (sandbox /tmp; requer IS_LOCAL=true).
        image_bytes_base64: bytes em base64 inline. Pouco prático em fluxo
            real porque o LLM trunca strings longas em tool args (>~10KB) —
            preferir salesforce_download_path quando vier do Salesforce.
        salesforce_download_path: caminho REST relativo do ContentVersion
            (ex: ``/services/data/v62.0/sobjects/ContentVersion/068.../VersionData``).
            Esta tool autentica via OAuth Client Credentials (Connected App)
            e baixa direto — sem truncation pelo LLM e sem precisar do
            Engine pré-fetch.
        message_id, content_version_id: correlação com register_inbound_media.

    Prioridade de fonte (primeiro que retornar bytes ganha):
        ``salesforce_download_path`` → ``image_bytes_base64`` →
        ``local_image_path``

    Returns:
        Dict com 'status', 'analysis' (descricao, categoria, workflow_sugerido,
        confianca etc), 'suggested_reply_pt_br' adaptado ao resultado.
    """
    if (file_extension or "").lower().lstrip(".") not in _ACCEPTED_EXTENSIONS:
        return {
            "status": "rejected",
            "error": f"extensão não suportada pra análise: {file_extension!r}",
            "accepted_extensions": sorted(_ACCEPTED_EXTENSIONS),
        }

    image_bytes: Optional[bytes] = None

    # 1) salesforce_download_path tem prioridade — caminho real-world em
    #    produção, sem dependência do LLM passar bytes inline. Usa wrapper
    #    async que offload o httpx sync pra thread, pra não bloquear o
    #    event loop do FastMCP.
    if salesforce_download_path:
        # Cross-check defensivo: `content_version_id` é OBRIGATÓRIO quando
        # `salesforce_download_path` é usado, e o Id embarcado no path tem
        # que bater com ele. Sem isso, um LLM (com prompt injection ou
        # marker stale) poderia apontar pro path de OUTRO arquivo —
        # incluindo omitir o `content_version_id` pra burlar o check.
        # Rejeitamos os 2 casos:
        #   - content_version_id ausente → skip download (não consigo
        #     correlacionar o path com o que o marker registra como audit)
        #   - Id no path ≠ content_version_id → skip download (divergência)
        if not content_version_id:
            logger.warning(
                "analyze_inbound_image: salesforce_download_path sem "
                "content_version_id pra cross-check; ignorando download "
                "pra evitar fetch de arquivo arbitrário."
            )
        elif not _path_matches_content_version_id(
            salesforce_download_path, content_version_id
        ):
            logger.warning(
                f"analyze_inbound_image: salesforce_download_path Id mismatch "
                f"vs content_version_id={content_version_id!r}; ignorando "
                f"download pra evitar fetch de arquivo divergente."
            )
        else:
            from src.utils.salesforce_client import download_content_version_async

            image_bytes = await download_content_version_async(salesforce_download_path)
            if image_bytes is None:
                logger.info(
                    "analyze_inbound_image: salesforce_download_path falhou; "
                    "caindo em image_bytes_base64/local_image_path se disponíveis."
                )

    # 2) image_bytes_base64 — alternativa pra testes ou Engine pré-fetch
    if image_bytes is None and image_bytes_base64:
        try:
            image_bytes = base64.b64decode(image_bytes_base64)
        except Exception as e:
            return {"status": "error", "error": f"base64 inválido: {e}"}
        # _read_image_bytes já aplica _MAX_BYTES em local_image_path; pra base64
        # validamos aqui depois do decode, antes de mandar pro Gemini.
        if len(image_bytes) > _MAX_BYTES:
            logger.warning(
                f"analyze_inbound_image: image_bytes_base64 {len(image_bytes)} "
                f"bytes > limite {_MAX_BYTES}"
            )
            return {
                "status": "rejected",
                "error": f"imagem excede {_MAX_BYTES} bytes (limite inline do Gemini)",
                "suggested_reply_pt_br": (
                    "Recebi sua imagem, mas ela está muito grande pra eu analisar. "
                    "Pode mandar uma foto com qualidade menor?"
                ),
            }

    # 3) local_image_path — sandbox /tmp em desenvolvimento
    if image_bytes is None and local_image_path:
        image_bytes = _read_image_bytes(local_image_path)

    if not image_bytes:
        return {
            "status": "deferred",
            "error": (
                "nenhuma fonte de bytes disponível: salesforce_download_path "
                "(faltam env vars ou download falhou), image_bytes_base64 "
                "(ausente/inválido), local_image_path (fora de sandbox/IS_LOCAL)"
            ),
            "suggested_reply_pt_br": (
                "Recebi sua imagem, mas não consegui analisá-la agora. "
                "Pode descrever em texto o que precisa pra eu te ajudar?"
            ),
        }

    if not env.GEMINI_API_KEY:
        logger.warning("analyze_inbound_image: GEMINI_API_KEY não configurada")
        return {
            "status": "deferred",
            "error": "GEMINI_API_KEY ausente",
            "suggested_reply_pt_br": (
                "Recebi sua imagem! Por enquanto, pode descrever em texto?"
            ),
        }

    client = genai.Client(api_key=env.GEMINI_API_KEY)
    mime = _mime_from_extension(file_extension)
    try:
        response = await client.aio.models.generate_content(
            model=_VISION_MODEL,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part(text=_ANALYSIS_PROMPT_PT_BR),
                        types.Part(
                            inline_data=types.Blob(mime_type=mime, data=image_bytes)
                        ),
                    ],
                )
            ],
        )
    except Exception as e:
        logger.error(f"analyze_inbound_image: Gemini falhou: {e}")
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "suggested_reply_pt_br": (
                "Recebi sua imagem, mas tive um problema pra analisar agora. "
                "Pode descrever em texto o que precisa?"
            ),
        }

    raw_text = (response.text or "").strip()
    analysis = _parse_analysis(raw_text)

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
