"""
Transcrição + classificação de áudio inbound do WhatsApp via Gemini multimodal.

Estende o `register_inbound_media` (audit-only stub) com análise do áudio
real via Gemini multimodal. O agente pode chamar AMBAS no mesmo turn:
  1. register_inbound_media → audit + ack
  2. analyze_inbound_audio  → transcrição → workflow apropriado

Caminhos de fonte de bytes (em ordem de preferência):
  - `meta_media_id` (canal canônico, ADR-017) → Graph API + Meta CDN
  - `salesforce_download_path` (UWC legacy, ADR-015) → SF REST OAuth
  - `audio_bytes_base64` (testes manuais)
  - `local_audio_path` (sandbox /tmp, IS_LOCAL=true)

Refator 2026-05-14 noite (ADR-018): bulk extraído pra
`src/utils/inbound_media_shared.py`. Este arquivo fica só com audio-specific:
prompt, allowlist, reply builder, MIME mapping.
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
    path_matches_content_version_id,
    rejected_subtype_mismatch,
    resolve_inbound_bytes,
)
from src.utils.log import logger

# Re-export pra backward compat de testes que importavam o helper privado.
# Função real vive em src.utils.inbound_media_shared agora (ADR-018 refator).
_path_matches_content_version_id = path_matches_content_version_id

_LOCAL_AUDIO_PATH_ALLOWED_PREFIX = Path("/tmp").resolve()

_AUDIO_MODEL = "gemini-2.5-flash"

# Allowlist Gemini audio input (https://ai.google.dev/gemini-api/docs/audio):
# WAV, MP3, AIFF, AAC, OGG, FLAC. M4A/AMR fora — viram `rejected` antes de
# Gemini call.
_ACCEPTED_EXTENSIONS = {"oga", "ogg", "aac", "mp3", "wav", "flac", "aiff", "aif"}

_MAX_BYTES = 20 * 1024 * 1024  # 20 MB — limite inline_data Gemini

# MIME do Graph API → extension canônica. Strip de codec/charset params no
# caller. PTT WhatsApp tipicamente vem como `audio/ogg; codecs=opus`.
_MIME_TO_EXT = {
    "audio/ogg": "ogg",
    "audio/opus": "ogg",  # opus container OGG
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/aac": "aac",
    "audio/x-aac": "aac",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "audio/aiff": "aiff",
    "audio/x-aiff": "aiff",
}

_ANALYSIS_PROMPT_PT_BR = """\
Você é um classificador de serviços da Prefeitura do Rio de Janeiro.

Um cidadão acabou de enviar essa mensagem de voz via WhatsApp. Transcreva
o áudio e classifique a intenção em JSON estrito (sem markdown, sem
comentários) com EXATAMENTE este schema:

{
  "transcricao": "<transcrição literal em PT-BR, sem editar gírias>",
  "resumo": "<resumo objetivo do pedido em 1-2 frases, max 240 chars>",
  "idioma_detectado": "<pt-br|pt-pt|es|en|outro>",
  "intencao_detectada": <true|false>,
  "categoria": "<um de: luminaria_publica | poda_arvore | buraco_via | lixo_irregular | iluminacao_publica | sinalizacao | endereco | duvida_geral | outro | nao_aplica>",
  "endereco_mencionado": "<rua/bairro/número se citado; '' se não>",
  "workflow_sugerido": "<um de: reparo_luminaria | poda_de_arvore | nenhum>",
  "confianca": "<alta|media|baixa>"
}

REGRAS:
- Transcreva exatamente o que ouvir. Não traduza. Se o áudio estiver
  ininteligível ou for ruído, use transcricao="" e intencao_detectada=false.
- Se o áudio relata luminária com problema (apagada, quebrada, queimada),
  workflow_sugerido="reparo_luminaria".
- Se relata árvore (galho caído, ameaçando fios, precisa podar),
  workflow_sugerido="poda_de_arvore".
- Pra outros relatos sem workflow MCP existente (buraco, lixo, etc.), use
  workflow_sugerido="nenhum" mas preencha categoria.
- Se o cidadão só responde com endereço sem pedir nada, categoria="endereco",
  workflow_sugerido="nenhum".
- Confiança "alta" se transcrição clara; "baixa" se houve muito ruído,
  fala cortada, ou múltiplas pessoas falando.
"""


def _read_audio_bytes(local_audio_path: Optional[str]) -> Optional[bytes]:
    """Lê bytes do arquivo local com sandbox de safety (espelho do vision)."""
    if not local_audio_path:
        return None
    if not env.IS_LOCAL:
        logger.warning(
            "analyze_inbound_audio: local_audio_path ignorado em produção "
            "(IS_LOCAL=false). Use salesforce_download_path."
        )
        return None
    p = local_audio_path
    if p.startswith("file://"):
        p = p[len("file://") :]
    path = Path(p).resolve()
    if not path.is_relative_to(_LOCAL_AUDIO_PATH_ALLOWED_PREFIX):
        logger.warning(
            f"analyze_inbound_audio: local_audio_path {path!s} fora do "
            f"prefixo permitido {_LOCAL_AUDIO_PATH_ALLOWED_PREFIX!s}; ignorado."
        )
        return None
    if not path.is_file():
        logger.warning(f"analyze_inbound_audio: arquivo não encontrado: {path}")
        return None
    size = path.stat().st_size
    if size > _MAX_BYTES:
        logger.warning(
            f"analyze_inbound_audio: arquivo {size} bytes > limite {_MAX_BYTES}"
        )
        return None
    return path.read_bytes()


def _mime_from_extension(file_extension: Optional[str]) -> str:
    """Extension canônica → MIME pro Gemini blob (inline_data audio).

    WhatsApp PTT chega como `.oga` (OGG/Opus mono 16kHz). Gemini documenta
    `audio/ogg` como suportado; empiricamente aceita também sem codec suffix.
    """
    ext = (file_extension or "oga").lower().lstrip(".")
    if ext in {"oga", "ogg"}:
        return "audio/ogg"
    if ext == "aac":
        return "audio/aac"
    if ext == "mp3":
        return "audio/mpeg"
    if ext == "wav":
        return "audio/wav"
    if ext == "flac":
        return "audio/flac"
    if ext in {"aiff", "aif"}:
        return "audio/aiff"
    return "audio/ogg"


def _build_reply_from_analysis(analysis: Dict[str, Any]) -> str:
    """Monta resposta amigável pro cidadão baseada na transcrição.

    Princípio: se a transcrição extraiu informação útil (intenção +
    eventualmente endereço), NÃO pedimos pro cidadão repetir o que já falou.
    O prompt module `audio_inbound` reforça o mesmo contrato no lado do LLM.
    """
    if not analysis.get("parsed"):
        return (
            "Recebi sua mensagem de voz, mas não consegui entender o conteúdo "
            "agora. Pode tentar de novo, ou me descrever em texto?"
        )
    transcricao = (analysis.get("transcricao") or "").strip()
    if not analysis.get("intencao_detectada") or not transcricao:
        return (
            "Recebi seu áudio, mas não consegui entender bem o que você precisa. "
            "Pode tentar de novo, ou me descrever em texto?"
        )
    workflow = analysis.get("workflow_sugerido", "nenhum")
    resumo = analysis.get("resumo") or transcricao[:200]
    endereco = (analysis.get("endereco_mencionado") or "").strip()
    if workflow == "reparo_luminaria":
        if endereco:
            return (
                f"Ouvi seu áudio: {resumo}. Vou abrir o chamado de reparo de "
                f"luminária no endereço que você mencionou ({endereco}) — "
                "confirma?"
            )
        return (
            f"Ouvi seu áudio: {resumo}. "
            "Vou te ajudar a abrir um chamado de reparo de luminária — "
            "você confirma que quer prosseguir? Me passa o endereço (rua, "
            "número, bairro)."
        )
    if workflow == "poda_de_arvore":
        if endereco:
            return (
                f"Ouvi seu áudio: {resumo}. Vou abrir a solicitação de poda no "
                f"endereço que você mencionou ({endereco}) — confirma?"
            )
        return (
            f"Ouvi seu áudio: {resumo}. "
            "Vou te ajudar a abrir uma solicitação de poda de árvore — "
            "você confirma? Me passa o endereço (rua, número, bairro)."
        )
    return f"Ouvi seu áudio: {resumo}. Vou seguir a partir disso."


async def analyze_inbound_audio(
    user_number: str,
    file_extension: Optional[str] = None,
    salesforce_download_path: Optional[str] = None,
    local_audio_path: Optional[str] = None,
    audio_bytes_base64: Optional[str] = None,
    meta_media_id: Optional[str] = None,
    message_id: Optional[str] = None,
    content_version_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Transcreve áudio inbound via Gemini multimodal e classifica intenção.

    Ver docstring do módulo pra fontes de bytes em ordem de preferência. A
    bulk da lógica vive em `src.utils.inbound_media_shared`.
    """
    source = await resolve_inbound_bytes(
        tool_name="analyze_inbound_audio",
        meta_media_id=meta_media_id,
        salesforce_download_path=salesforce_download_path,
        content_version_id=content_version_id,
        local_path=local_audio_path,
        bytes_base64=audio_bytes_base64,
        file_extension=file_extension,
        accepted_extensions=_ACCEPTED_EXTENSIONS,
        mime_to_extension=_MIME_TO_EXT,
        max_bytes=_MAX_BYTES,
        media_domain="audio",
        local_path_reader=_read_audio_bytes,
    )
    if source.error_response is not None:
        return source.error_response
    audio_bytes = source.image_bytes  # MediaSourceResult.image_bytes é genérico
    file_extension = source.file_extension

    if not audio_bytes:
        return deferred_no_bytes("audio")

    # Anti-hallucination magic-byte check (ver inbound_media_shared docstring).
    # Áudio em particular sofreu hallucination determinística no smoke test
    # 2026-05-14 com JPG enviado como audio/ogg.
    from src.utils.media_sniff import detect_media_subtype, matches_expected_extension

    if not matches_expected_extension(audio_bytes, file_extension):
        detected_subtype = detect_media_subtype(audio_bytes) or "unknown"
        logger.warning(
            f"analyze_inbound_audio: subtype dos magic bytes não bate com a "
            f"extension declarada (detected_subtype={detected_subtype!r}, "
            f"declared file_extension={file_extension!r}, "
            f"first_bytes={audio_bytes[:8]!r}, message_id={message_id}, "
            f"content_version_id={content_version_id})"
        )
        return rejected_subtype_mismatch(
            detected=detected_subtype,
            declared=file_extension or "",
            message_id=message_id,
            media_domain="audio",
        )

    if not env.GEMINI_API_KEY:
        logger.warning("analyze_inbound_audio: GEMINI_API_KEY não configurada")
        return deferred_no_gemini_key()

    client = genai.Client(api_key=env.GEMINI_API_KEY)
    mime = _mime_from_extension(file_extension)
    gemini_result = await call_gemini_with_blob(
        client=client,
        model=_AUDIO_MODEL,
        prompt_text=_ANALYSIS_PROMPT_PT_BR,
        mime_type=mime,
        blob_bytes=audio_bytes,
        tool_name="analyze_inbound_audio",
    )
    if gemini_result.text is None:
        return error_gemini_failed(
            gemini_result.error_detail or "Gemini call failed", "audio"
        )

    analysis = parse_analysis_json(
        gemini_result.text, tool_name="analyze_inbound_audio"
    )
    suggested_reply = _build_reply_from_analysis(analysis)

    logger.info(
        f"analyze_inbound_audio: user_number={user_number} "
        f"categoria={analysis.get('categoria')!r} "
        f"workflow={analysis.get('workflow_sugerido')!r} "
        f"confianca={analysis.get('confianca')!r} "
        f"message_id={message_id} content_version_id={content_version_id} "
        f"transcricao_len={len((analysis.get('transcricao') or ''))}"
    )

    return {
        "status": "transcribed",
        "analysis": analysis,
        "suggested_reply_pt_br": suggested_reply,
    }
