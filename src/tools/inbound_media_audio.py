"""
Transcrição + classificação de áudio inbound do WhatsApp via Gemini multimodal.

Espelha o desenho de :mod:`inbound_media_vision`: a tool baixa bytes
direto do Salesforce REST (ContentVersion ``VersionData``) via OAuth Client
Credentials, manda pro Gemini com ``inline_data`` (mime ``audio/ogg`` etc.)
e retorna ``{transcricao, intencao_detectada, workflow_sugerido, ...}``.

Caminho real-world (produção):
  Apex correlaciona ContentVersion do PTT do WhatsApp (FileExtension=oga,
  FileType=UNKNOWN — UWC não popula MIME)
    → Mule encaminha ``message_type=audio`` + ``media.download_path`` no
      webhook do Gateway
    → Gateway compõe ``[INBOUND_MEDIA] type=audio media={download_path,...}``
    → Engine LLM chama ``register_inbound_media`` (audit) +
      ``analyze_inbound_audio`` (esta tool, com ``salesforce_download_path``).

Modos alternativos pra testes locais:
  ``audio_bytes_base64`` (LLM trunca >~10KB, pouco útil em prod) e
  ``local_audio_path`` (sandbox /tmp + IS_LOCAL=true).
"""

import base64
from pathlib import Path
from typing import Any, Dict, Optional

from google import genai
from google.genai import types

from src.config import env
from src.utils.log import logger

# Diretório seguro pra `local_audio_path` (sandbox de testes locais). Mesma
# convenção de :mod:`inbound_media_vision` — paths fora desse prefixo são
# rejeitados pra evitar arbitrary file read via prompt injection no MCP tool.
_LOCAL_AUDIO_PATH_ALLOWED_PREFIX = Path("/tmp").resolve()

_AUDIO_MODEL = "gemini-2.5-flash"

# Extensões aceitas. WhatsApp PTT chega como `.oga` (Opus mono 16kHz dentro
# de OGG). Bridge UWC pode também emitir `.ogg`, `.aac`, `.mp3`, `.wav`, `.flac`,
# `.aiff` dependendo do tipo de áudio enviado pelo cidadão.
#
# IMPORTANTE: a lista é restrita aos formatos documentados pelo Gemini audio
# input (WAV, MP3, AIFF, AAC, OGG, FLAC — https://ai.google.dev/gemini-api/docs/audio).
# Containers como AMR (codec deprecado, comum em SMS via gateways) ou M4A
# (MP4 audio-only) NÃO estão no allowlist do Gemini inline_data — se chegarem
# do bridge UWC, viram `rejected` aqui em vez de passar pra um erro Gemini
# downstream. Trocar/transcodar fica como evolução futura.
_ACCEPTED_EXTENSIONS = {"oga", "ogg", "aac", "mp3", "wav", "flac", "aiff", "aif"}

# Limite do `inline_data` do Gemini (mesma fronteira de imagem). PTT
# WhatsApp tipicamente ~15-60 KB; áudios mais longos do menu de anexo
# raramente passam de poucos MB. 20 MB cobre com folga.
_MAX_BYTES = 20 * 1024 * 1024

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


def _path_matches_content_version_id(path: str, content_version_id: str) -> bool:
    """Cross-check do Id no path vs content_version_id do marker. Idêntico
    ao :func:`inbound_media_vision._path_matches_content_version_id`."""
    if not path or not content_version_id:
        return False
    try:
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
    """
    Mapeia extensão pra MIME aceito pelo Gemini ``inline_data`` (audio).

    WhatsApp PTT chega como ``.oga`` (OGG container, codec Opus mono). Gemini
    documenta ``audio/ogg`` como suportado — empiricamente também aceita o
    container OGG mesmo sem o sufixo ``;codecs=opus``.

    Mantemos o domínio restrito aos formatos que o Gemini audio input aceita
    (https://ai.google.dev/gemini-api/docs/audio): WAV, MP3, AIFF, AAC, OGG,
    FLAC. O allowlist em :data:`_ACCEPTED_EXTENSIONS` já guarda a entrada;
    aqui é só o mapeamento ext → MIME.
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
    return "audio/ogg"  # default razoável p/ PTT WhatsApp


def _parse_analysis(text: str) -> Dict[str, Any]:
    """Espelho de :func:`inbound_media_vision._parse_analysis`."""
    import json
    import re

    if not text:
        return {"raw": text, "parsed": False}
    cleaned = text.strip()
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
    logger.warning(f"analyze_inbound_audio: JSON inválido do Gemini: {text[:200]}")
    return {"raw": text, "parsed": False}


async def analyze_inbound_audio(
    user_number: str,
    file_extension: str,
    salesforce_download_path: Optional[str] = None,
    local_audio_path: Optional[str] = None,
    audio_bytes_base64: Optional[str] = None,
    message_id: Optional[str] = None,
    content_version_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Transcreve áudio inbound via Gemini multimodal e classifica intenção.

    Args:
        user_number: telefone E.164 sem '+'.
        file_extension: 'oga' | 'ogg' | 'm4a' | 'aac' | 'amr' | 'mp3' | 'wav'.
        salesforce_download_path: caminho REST relativo do ContentVersion (ex:
            ``/services/data/v62.0/sobjects/ContentVersion/068.../VersionData``).
            Tool autentica via OAuth Client Credentials e baixa direto — sem
            truncation pelo LLM, sem Engine pré-fetch.
        local_audio_path: sandbox /tmp pra testes locais (requer IS_LOCAL=true).
        audio_bytes_base64: bytes inline (raro em produção; LLM trunca).
        message_id, content_version_id: correlação com register_inbound_media.

    Prioridade de fonte: salesforce_download_path > audio_bytes_base64 >
    local_audio_path.

    Returns:
        Dict com 'status', 'analysis' (transcricao, resumo, categoria,
        intencao_detectada, workflow_sugerido, confianca etc),
        'suggested_reply_pt_br' adaptado ao resultado.
    """
    if (file_extension or "").lower().lstrip(".") not in _ACCEPTED_EXTENSIONS:
        return {
            "status": "rejected",
            "error": f"extensão não suportada pra transcrição: {file_extension!r}",
            "accepted_extensions": sorted(_ACCEPTED_EXTENSIONS),
        }

    audio_bytes: Optional[bytes] = None

    # 1) salesforce_download_path (prioritário em prod). Mesma defesa anti
    # prompt-injection do vision: content_version_id obrigatório, Id no path
    # tem que bater com ele.
    if salesforce_download_path:
        if not content_version_id:
            logger.warning(
                "analyze_inbound_audio: salesforce_download_path sem "
                "content_version_id pra cross-check; ignorando download "
                "pra evitar fetch de arquivo arbitrário."
            )
        elif not _path_matches_content_version_id(
            salesforce_download_path, content_version_id
        ):
            logger.warning(
                f"analyze_inbound_audio: salesforce_download_path Id mismatch "
                f"vs content_version_id={content_version_id!r}; ignorando "
                f"download pra evitar fetch de arquivo divergente."
            )
        else:
            from src.utils.salesforce_client import download_content_version_async

            audio_bytes = await download_content_version_async(salesforce_download_path)
            if audio_bytes is None:
                logger.info(
                    "analyze_inbound_audio: salesforce_download_path falhou; "
                    "caindo em audio_bytes_base64/local_audio_path se disponíveis."
                )

    # 2) audio_bytes_base64 — alternativa pra testes
    if audio_bytes is None and audio_bytes_base64:
        # Anti-OOM: o base64 expande bytes em ~4/3, então `encoded_len * 3 / 4`
        # é um upper bound do tamanho decoded. Rejeitamos ANTES do decode pra
        # não materializar buffer arbitrariamente grande (DoS-style — sem isso
        # o servidor poderia alocar centenas de MB só pra rejeitar em seguida).
        approx_decoded = (len(audio_bytes_base64) * 3) // 4
        if approx_decoded > _MAX_BYTES:
            logger.warning(
                f"analyze_inbound_audio: audio_bytes_base64 encoded={len(audio_bytes_base64)} "
                f"(~{approx_decoded} bytes decoded) > limite {_MAX_BYTES}; abort pre-decode"
            )
            return {
                "status": "rejected",
                "error": f"áudio excede {_MAX_BYTES} bytes (limite inline do Gemini)",
                "suggested_reply_pt_br": (
                    "Recebi sua mensagem de voz, mas ela está muito longa pra eu "
                    "ouvir agora. Pode mandar um áudio mais curto ou escrever em texto?"
                ),
            }
        try:
            audio_bytes = base64.b64decode(audio_bytes_base64)
        except Exception as e:
            return {"status": "error", "error": f"base64 inválido: {e}"}
        if len(audio_bytes) > _MAX_BYTES:
            logger.warning(
                f"analyze_inbound_audio: audio_bytes_base64 {len(audio_bytes)} "
                f"bytes > limite {_MAX_BYTES}"
            )
            return {
                "status": "rejected",
                "error": f"áudio excede {_MAX_BYTES} bytes (limite inline do Gemini)",
                "suggested_reply_pt_br": (
                    "Recebi sua mensagem de voz, mas ela está muito longa pra eu "
                    "ouvir agora. Pode mandar um áudio mais curto ou escrever em texto?"
                ),
            }

    # 3) local_audio_path — sandbox /tmp em desenvolvimento
    if audio_bytes is None and local_audio_path:
        audio_bytes = _read_audio_bytes(local_audio_path)

    if not audio_bytes:
        return {
            "status": "deferred",
            "error": (
                "nenhuma fonte de bytes disponível: salesforce_download_path "
                "(faltam env vars ou download falhou), audio_bytes_base64 "
                "(ausente/inválido), local_audio_path (fora de sandbox/IS_LOCAL)"
            ),
            "suggested_reply_pt_br": (
                "Recebi seu áudio, mas não consegui ouvir agora. "
                "Pode escrever em texto o que precisa pra eu te ajudar?"
            ),
        }

    if not env.GEMINI_API_KEY:
        logger.warning("analyze_inbound_audio: GEMINI_API_KEY não configurada")
        return {
            "status": "deferred",
            "error": "GEMINI_API_KEY ausente",
            "suggested_reply_pt_br": (
                "Recebi sua mensagem de voz! Por enquanto, pode escrever em texto?"
            ),
        }

    client = genai.Client(api_key=env.GEMINI_API_KEY)
    mime = _mime_from_extension(file_extension)
    try:
        response = await client.aio.models.generate_content(
            model=_AUDIO_MODEL,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part(text=_ANALYSIS_PROMPT_PT_BR),
                        types.Part(
                            inline_data=types.Blob(mime_type=mime, data=audio_bytes)
                        ),
                    ],
                )
            ],
        )
    except Exception as e:
        logger.error(f"analyze_inbound_audio: Gemini falhou: {e}")
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "suggested_reply_pt_br": (
                "Recebi seu áudio, mas tive um problema pra ouvir agora. "
                "Pode escrever em texto o que precisa?"
            ),
        }

    raw_text = (response.text or "").strip()
    analysis = _parse_analysis(raw_text)

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


def _build_reply_from_analysis(analysis: Dict[str, Any]) -> str:
    """Monta resposta amigável pro cidadão baseada na transcrição.

    Princípio: se a transcrição extraiu informação útil (intenção +
    eventualmente endereço), NÃO pedimos pro cidadão repetir o que já
    falou. O prompt module ``audio_inbound`` reforça o mesmo contrato no
    lado do LLM. Isso evita fricção desnecessária ("você acabou de me
    dizer, e agora quer que eu digite de novo?").
    """
    if not analysis.get("parsed"):
        # Sem JSON parseável só sobra o fallback genérico — não temos
        # transcrição confiável pra reaproveitar.
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
    # workflow_sugerido='nenhum': transcrição é a mensagem real do cidadão.
    # Não pedimos pra repetir — o LLM downstream continua o fluxo usando
    # ``analysis.transcricao`` (instrução reforçada em audio_inbound prompt
    # module). Mensagem aqui só ecoa o que entendemos pra dar ack.
    return f"Ouvi seu áudio: {resumo}. Vou seguir a partir disso."
