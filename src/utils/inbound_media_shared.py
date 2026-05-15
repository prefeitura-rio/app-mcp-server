"""
Helpers compartilhados pra tools de análise de mídia inbound
(`analyze_inbound_image`, `analyze_inbound_audio` e futuros tipos).

Antes deste módulo, vision e audio tinham ~70% de código duplicado: download
resolver multi-source, cross-check Salesforce path, magic-byte sniff,
chamada Gemini, parse JSON. Esta refatoração (ADR-018, 2026-05-14 noite)
extrai a parte comum mantendo o adapter pequeno por tipo de mídia.

Adicionar um novo tipo de mídia analisável (ex: video) vira:
  1. Definir `MediaTypeConfig` em `src/tools/inbound_media_<tipo>.py`
  2. Chamar `resolve_inbound_bytes(...)` + `call_gemini_analysis(...)` +
     `parse_analysis_json(...)`
  3. Expor wrapper FastMCP em `src/app.py`

Não toca em nada se o tipo de mídia não é analisável (text, location,
unsupported) — esses ficam só com `register_inbound_media`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Set

from src.utils.log import logger


# ----------------------------------------------------------------------------
# Salesforce path cross-check (anti prompt-injection)
# ----------------------------------------------------------------------------


def path_matches_content_version_id(path: str, content_version_id: str) -> bool:
    """Cross-check: extrai o Id embarcado no salesforce_download_path e
    confirma que bate com o content_version_id do marker.

    Salesforce ContentVersion Ids têm forma 15- ou 18-char alfanuméricos.
    O 15-char é prefix do 18-char (sufixo 3-char é checksum case-sensitive).
    Comparamos só os primeiros 15 chars — independente de qual lado é 15 ou 18.

    Anti prompt-injection: sem este check, um LLM (com marker stale ou
    prompt-injected) poderia apontar pro path de OUTRO arquivo.
    """
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


# ----------------------------------------------------------------------------
# Gemini response parsing
# ----------------------------------------------------------------------------


def parse_analysis_json(
    text: str, tool_name: str = "analyze_inbound_media"
) -> Dict[str, Any]:
    """Tenta extrair JSON da resposta do Gemini com fallback se vier prosa.

    Strip de fences markdown (```json ... ```), 2 tentativas: parse direto +
    regex-extract do primeiro objeto JSON. Retorna `{parsed: False, raw: ...}`
    se nenhuma der.
    """
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
    logger.warning(f"{tool_name}: JSON inválido do Gemini: {text[:200]}")
    return {"raw": text, "parsed": False}


# ----------------------------------------------------------------------------
# Byte source resolution (priority: meta_media_id > sf_path > base64 > local)
# ----------------------------------------------------------------------------


def _meta_cdn_failure_reply(media_domain: str) -> str:
    """Texto pro cidadão quando Meta CDN não retornou bytes/MIME."""
    if media_domain == "image":
        return (
            "Recebi sua imagem, mas tive um problema pra acessá-la agora. "
            "Pode descrever em texto o que precisa pra eu te ajudar?"
        )
    if media_domain == "audio":
        return (
            "Recebi sua mensagem de voz, mas tive um problema pra acessá-la "
            "agora. Pode descrever em texto o que precisa pra eu te ajudar?"
        )
    if media_domain == "video":
        return (
            "Recebi seu vídeo, mas tive um problema pra acessá-lo agora. "
            "Pode descrever em texto o que precisa pra eu te ajudar?"
        )
    return (
        "Recebi sua mídia, mas tive um problema pra acessá-la agora. "
        "Pode descrever em texto o que precisa pra eu te ajudar?"
    )


@dataclass(frozen=True)
class MediaSourceResult:
    """Output do resolver: (bytes ou None, file_extension derivada ou original,
    error_response_dict pra caller short-circuitar antes do Gemini call)."""

    image_bytes: Optional[bytes]
    file_extension: Optional[str]
    error_response: Optional[Dict[str, Any]]


async def resolve_inbound_bytes(
    *,
    tool_name: str,
    meta_media_id: Optional[str],
    salesforce_download_path: Optional[str],
    content_version_id: Optional[str],
    local_path: Optional[str],
    bytes_base64: Optional[str],
    file_extension: Optional[str],
    accepted_extensions: Set[str],
    mime_to_extension: Mapping[str, str],
    max_bytes: int,
    media_domain: str,  # "image" ou "audio" — só pra mensagens de fallback
    local_path_reader: Callable[[Optional[str]], Optional[bytes]],
) -> MediaSourceResult:
    """Resolve bytes da mídia a partir das múltiplas fontes possíveis,
    com prioridade Meta CDN > Salesforce > base64 > local file.

    Cobre:
      - Meta CDN (ADR-017) — download via Graph API com WA_TOKEN
      - Salesforce ContentVersion (ADR-014/015) — OAuth Client Credentials
      - Inline base64 (testes)
      - Local /tmp (IS_LOCAL)

    Deriva `file_extension` automaticamente do MIME real Meta CDN quando
    presente; caller pode passar None pra esse argumento se vier do Meta.

    Returns:
      - `bytes`: bytes da mídia (None se nenhuma fonte funcionou)
      - `file_extension`: extensão canônica derivada/normalizada (lowercase
        sem ponto)
      - `error_response`: dict pronto pra retorno do caller quando algo
        bloqueante aconteceu (allowlist fail, Meta CDN fail Meta-only,
        bytes overlimit). Caller faz `if r.error_response: return r.error_response`.
    """
    image_bytes: Optional[bytes] = None

    # 1) meta_media_id (Caminho canônico ADR-017)
    if meta_media_id:
        from src.utils.meta_cdn_client import MetaCDNError, download_meta_media

        try:
            image_bytes, _meta_mime = await download_meta_media(meta_media_id)
        except MetaCDNError as exc:
            logger.warning(
                f"{tool_name}: download Meta CDN falhou "
                f"(meta_media_id={meta_media_id!r}): {exc}; "
                f"tentando fallback (salesforce_download_path/base64/local)."
            )
        else:
            # MIME do Graph API é autoritativo; strip codec/charset params
            mime_clean = (_meta_mime or "").split(";")[0].strip().lower()
            ext_from_mime = mime_to_extension.get(mime_clean)
            if ext_from_mime:
                file_extension = ext_from_mime
            elif not file_extension:
                logger.warning(
                    f"{tool_name}: MIME Meta CDN inesperado ({_meta_mime!r}) "
                    f"e file_extension ausente; rejeitando."
                )
                return MediaSourceResult(
                    image_bytes=None,
                    file_extension=file_extension,
                    error_response={
                        "status": "rejected",
                        "error": (
                            f"MIME Meta CDN não suportado pra {media_domain}: "
                            f"{_meta_mime!r}."
                        ),
                        "accepted_extensions": sorted(accepted_extensions),
                    },
                )

    # 2) Meta-only call falhou no Caminho A — retorna deferred (operacional)
    #    Sem isso, allowlist check abaixo retornaria "rejected" pra caller-fault
    #    quando o problema foi infra (token/timeout/CDN).
    if meta_media_id and not file_extension and image_bytes is None:
        logger.info(
            f"{tool_name}: Meta CDN download falhou pra "
            f"meta_media_id={meta_media_id!r} sem file_extension; deferred."
        )
        return MediaSourceResult(
            image_bytes=None,
            file_extension=None,
            error_response={
                "status": "deferred",
                "error": (
                    "Meta CDN não retornou bytes nem MIME "
                    "(token/timeout/expired); sem fallback configurado."
                ),
                "suggested_reply_pt_br": _meta_cdn_failure_reply(media_domain),
            },
        )

    # 3) Allowlist check (rejeita extensions fora do que a Gemini API suporta)
    ext_norm = (file_extension or "").lower().lstrip(".")
    if ext_norm not in accepted_extensions:
        return MediaSourceResult(
            image_bytes=None,
            file_extension=file_extension,
            error_response={
                "status": "rejected",
                "error": f"extensão não suportada pra {media_domain}: {file_extension!r}",
                "accepted_extensions": sorted(accepted_extensions),
            },
        )

    # 4) Fallbacks só rodam se meta_media_id ausente ou seu download falhou
    if image_bytes is None and salesforce_download_path:
        # Anti prompt-injection: content_version_id obrigatório + Id no path
        # deve bater com ele.
        if not content_version_id:
            logger.warning(
                f"{tool_name}: salesforce_download_path sem content_version_id "
                f"pra cross-check; ignorando download."
            )
        elif not path_matches_content_version_id(
            salesforce_download_path, content_version_id
        ):
            logger.warning(
                f"{tool_name}: salesforce_download_path Id mismatch vs "
                f"content_version_id={content_version_id!r}; ignorando."
            )
        else:
            from src.utils.salesforce_client import download_content_version_async

            image_bytes = await download_content_version_async(salesforce_download_path)
            if image_bytes is None:
                logger.info(
                    f"{tool_name}: salesforce_download_path falhou; "
                    f"caindo em bytes_base64/local."
                )

    # 5) base64 inline (testes)
    if image_bytes is None and bytes_base64:
        import base64 as _b64

        approx_decoded = (len(bytes_base64) * 3) // 4
        if approx_decoded > max_bytes:
            logger.warning(
                f"{tool_name}: bytes_base64 encoded={len(bytes_base64)} "
                f"(~{approx_decoded} bytes decoded) > limite {max_bytes}; "
                f"abort pre-decode."
            )
            return MediaSourceResult(
                image_bytes=None,
                file_extension=file_extension,
                error_response={
                    "status": "rejected",
                    "error": f"mídia excede {max_bytes} bytes (limite Gemini)",
                    "suggested_reply_pt_br": (
                        "Recebi sua mensagem, mas o arquivo está muito grande "
                        "pra eu processar. Pode mandar algo menor ou em texto?"
                    ),
                },
            )
        try:
            image_bytes = _b64.b64decode(bytes_base64)
        except Exception as e:
            return MediaSourceResult(
                image_bytes=None,
                file_extension=file_extension,
                error_response={"status": "error", "error": f"base64 inválido: {e}"},
            )
        if len(image_bytes) > max_bytes:
            return MediaSourceResult(
                image_bytes=None,
                file_extension=file_extension,
                error_response={
                    "status": "rejected",
                    "error": f"mídia excede {max_bytes} bytes (limite Gemini)",
                },
            )

    # 6) local path (sandbox)
    if image_bytes is None and local_path:
        image_bytes = local_path_reader(local_path)

    return MediaSourceResult(
        image_bytes=image_bytes,
        file_extension=ext_norm or file_extension,
        error_response=None,
    )


# ----------------------------------------------------------------------------
# Gemini call wrapper
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class GeminiCallResult:
    """Output de `call_gemini_with_blob`: response text OU detalhe do erro.

    Quando `text` é None, `error_detail` carrega `{type}: {message}` da
    exception pra caller compor `error_gemini_failed`. Preserva o contrato
    de erro pre-refator (ADR-018) que mostrava qual tipo de exceção
    aconteceu — operadores usam isso pra distinguir quota/timeout/bad
    request transientes vs bugs.
    """

    text: Optional[str]
    error_detail: Optional[str]


async def call_gemini_with_blob(
    *,
    client: Any,
    model: str,
    prompt_text: str,
    mime_type: str,
    blob_bytes: bytes,
    tool_name: str,
) -> GeminiCallResult:
    """Faz uma chamada ao Gemini multimodal com texto + blob inline.

    Returns:
        `GeminiCallResult(text=...)` em sucesso ou
        `GeminiCallResult(text=None, error_detail="<ExcType>: <msg>")` em falha.

    Caller decide o `model` (ex: ``gemini-2.5-flash``) e `mime_type`
    (ex: ``image/jpeg``, ``audio/ogg``). Erros são logados; caller deve
    propagar `error_detail` pra `error_gemini_failed` pra manter
    contrato de observabilidade.
    """
    from google.genai import types

    try:
        response = await client.aio.models.generate_content(
            model=model,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part(text=prompt_text),
                        types.Part(
                            inline_data=types.Blob(mime_type=mime_type, data=blob_bytes)
                        ),
                    ],
                )
            ],
        )
        return GeminiCallResult(text=(response.text or "").strip(), error_detail=None)
    except Exception as e:  # noqa: BLE001 — caller cuida do fallback
        detail = f"{type(e).__name__}: {e}"
        logger.error(f"{tool_name}: Gemini falhou: {detail}")
        return GeminiCallResult(text=None, error_detail=detail)


# ----------------------------------------------------------------------------
# Common deferred-response helpers
# ----------------------------------------------------------------------------


def deferred_no_bytes(media_domain: str) -> Dict[str, Any]:
    """Resposta padrão quando nenhuma fonte de bytes funcionou."""
    if media_domain == "image":
        suggested = (
            "Recebi sua imagem, mas não consegui analisá-la agora. "
            "Pode descrever em texto o que precisa pra eu te ajudar?"
        )
    elif media_domain == "audio":
        suggested = (
            "Recebi seu áudio, mas não consegui ouvir agora. "
            "Pode escrever em texto o que precisa pra eu te ajudar?"
        )
    elif media_domain == "video":
        suggested = (
            "Recebi seu vídeo, mas não consegui processá-lo agora. "
            "Pode descrever em texto o que precisa pra eu te ajudar?"
        )
    else:
        suggested = (
            "Recebi sua mídia, mas não consegui processá-la agora. "
            "Pode descrever em texto o que precisa pra eu te ajudar?"
        )
    return {
        "status": "deferred",
        "error": (
            "nenhuma fonte de bytes disponível: meta_media_id "
            "(token/CDN falhou), salesforce_download_path (faltam env vars "
            "ou download falhou), bytes_base64 (ausente/inválido), "
            "local_path (fora de sandbox/IS_LOCAL)"
        ),
        "suggested_reply_pt_br": suggested,
    }


def rejected_subtype_mismatch(
    *, detected: str, declared: str, message_id: Optional[str], media_domain: str
) -> Dict[str, Any]:
    """Resposta padrão quando magic-byte verification falha.

    Anti-hallucination: Gemini com bytes errados pode alucinar análise
    plausível (ver `media_sniff.py`). Rejeitar antes do Gemini call.
    """
    if media_domain == "image":
        suggested = (
            "Recebi sua mensagem, mas o arquivo de imagem não chegou em um "
            "formato que eu consigo analisar. Pode me descrever em texto?"
        )
    elif media_domain == "audio":
        suggested = (
            "Recebi sua mensagem, mas o arquivo de áudio não chegou em um "
            "formato que eu consigo entender. Pode me descrever em texto?"
        )
    elif media_domain == "video":
        suggested = (
            "Recebi sua mensagem, mas o arquivo de vídeo não chegou em um "
            "formato que eu consigo analisar. Pode me descrever em texto?"
        )
    else:
        suggested = (
            "Recebi sua mensagem, mas o anexo não chegou em um formato "
            "que eu consigo processar. Pode me descrever em texto?"
        )
    return {
        "status": "rejected",
        "error": (
            f"bytes baixados não batem com extension declarada "
            f"(detected={detected!r}, declared={declared!r}). "
            f"Rejeitando pra evitar análise alucinada pelo Gemini."
        ),
        "suggested_reply_pt_br": suggested,
    }


def deferred_no_gemini_key() -> Dict[str, Any]:
    """Resposta quando GEMINI_API_KEY não configurada."""
    return {
        "status": "deferred",
        "error": "GEMINI_API_KEY ausente",
        "suggested_reply_pt_br": (
            "Recebi sua mensagem! Por enquanto, pode descrever em texto?"
        ),
    }


def error_gemini_failed(exception_str: str, media_domain: str) -> Dict[str, Any]:
    """Resposta quando Gemini call falhou (network/quota/etc)."""
    if media_domain == "image":
        suggested = (
            "Recebi sua imagem, mas tive um problema pra analisar agora. "
            "Pode descrever em texto?"
        )
    elif media_domain == "audio":
        suggested = (
            "Recebi sua mensagem de voz, mas tive um problema pra ouvir agora. "
            "Pode escrever em texto?"
        )
    else:
        suggested = (
            "Recebi sua mensagem, mas tive um problema pra processá-la agora. "
            "Pode descrever em texto?"
        )
    return {
        "status": "error",
        "error": exception_str,
        "suggested_reply_pt_br": suggested,
    }


# ----------------------------------------------------------------------------
# Media type configuration registry
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class MediaTypeConfig:
    """Configuração imutável por tipo de mídia analisável.

    Cada `analyze_inbound_<tipo>` tool define a sua instância e passa pros
    helpers. Adicionar suporte a vídeo (futuro) = criar novo
    `MediaTypeConfig(domain="video", ...)` + tool wrapper.
    """

    domain: str  # "image" | "audio" | (futuro: "video", "document")
    accepted_extensions: frozenset
    mime_to_extension: Mapping[str, str]
    extension_to_mime: Callable[[Optional[str]], str]
    max_bytes: int
    gemini_model: str
    analysis_prompt: str
    suggested_reply_builder: Callable[[Dict[str, Any]], str]
