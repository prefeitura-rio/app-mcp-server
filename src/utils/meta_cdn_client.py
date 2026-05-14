"""
Meta CDN client — download de mídia inbound recebida via webhook direto Meta.

Pipeline 2-step (formato estável Graph API v23.0):

  1. GET https://graph.facebook.com/v23.0/<media_id>?access_token=<TOKEN>
       → 200 OK { url, mime_type, sha256, file_size, messaging_product, id }
       → url é assinada, expira em ~5min, hostada em lookaside.fbsbx.com

  2. GET <url> com Authorization: Bearer <TOKEN>
       → 200 OK <bytes>

Usado por `analyze_inbound_image` e `analyze_inbound_audio` quando o
inbound vem do `meta-webhook-flow.xml` no Mule (caminho não-BSP, ADR-017)
em vez do `salesforce_download_path` (caminho UWC legacy).

Token: `WA_TOKEN` env var, mesmo já usado por `whatsapp_flow_sender.py`.
Scopes necessários: `whatsapp_business_messaging` (suficiente pra ler URL
+ baixar bytes; o token do Bruno em 2026-05-14 tem ambos).

Defesas:
  - Timeout 10s na metadata + 30s no download (audio/video pode ser
    grande, mas Meta cap é 16MB).
  - File size validation (envia hint pro caller fazer cap).
  - Magic-byte verification fica responsabilidade do caller (igual ao
    salesforce_download_path path — `media_sniff.matches_expected_extension`).
  - URL não-logada (assinada, contém access info).
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

import httpx
from loguru import logger

from src.config import env

_GRAPH_API_VERSION = "v23.0"
_METADATA_TIMEOUT_S = 10.0
_DOWNLOAD_TIMEOUT_S = 30.0
_MAX_BYTES = 17 * 1024 * 1024  # 17MB hard cap (Meta limit ~16MB pra audio/video)

# Meta media IDs são strings numéricas (15-20 dígitos hoje).
# Validação anti prompt-injection: meta_media_id chega via tool args
# controlados pelo LLM. Sem isso, `meta_media_id="../../me"` faria httpx
# normalizar URL pra /v23.0/me e o servidor faria GET autenticado em
# endpoint não-media. Codex P2 fix 2026-05-14.
_META_MEDIA_ID_RE = re.compile(r"^[0-9]{1,32}$")


class MetaCDNError(Exception):
    """Erro de download Meta CDN. Caller deve fallback gracefully."""


async def fetch_media_url(
    meta_media_id: str,
    token: Optional[str] = None,
) -> Tuple[str, str, int]:
    """
    Step 1: pega URL assinada + metadata pra um meta_media_id.

    Returns:
        (signed_url, mime_type, file_size_bytes)

    Raises:
        MetaCDNError: quando token ausente, 4xx/5xx do Graph API, ou
        resposta sem `url` (formato inesperado).
    """
    token = token or getattr(env, "WA_TOKEN", None)
    if not token:
        raise MetaCDNError("WA_TOKEN não configurado")
    if not meta_media_id:
        raise MetaCDNError("meta_media_id vazio")
    if not _META_MEDIA_ID_RE.match(meta_media_id):
        # Bloqueia path traversal / query-string injection. LLM pode tentar
        # `../../me`, `?fields=...`, `123/files`. Format strict 1-32 dígitos.
        raise MetaCDNError(
            f"meta_media_id formato inválido (esperado [0-9]{{1,32}}): "
            f"{meta_media_id!r}"
        )

    metadata_url = f"https://graph.facebook.com/{_GRAPH_API_VERSION}/{meta_media_id}"
    async with httpx.AsyncClient(timeout=_METADATA_TIMEOUT_S) as client:
        try:
            resp = await client.get(
                metadata_url,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.TimeoutException as exc:
            raise MetaCDNError(
                f"timeout no metadata GET ({_METADATA_TIMEOUT_S}s)"
            ) from exc
        except httpx.HTTPError as exc:
            raise MetaCDNError(f"http error no metadata GET: {exc}") from exc

    if resp.status_code != 200:
        body_preview = resp.text[:200] if resp.text else ""
        raise MetaCDNError(f"metadata GET retornou {resp.status_code}: {body_preview}")

    # Wrap JSON parse + shape validation em MetaCDNError pra que callers que
    # só pegam MetaCDNError não vejam exceção fora do contrato. Proxy/erro
    # transient page pode retornar 200 + HTML, ou shape inesperado.
    try:
        data = resp.json()
    except Exception as exc:
        raise MetaCDNError(
            f"metadata response JSON inválido: {type(exc).__name__}"
        ) from exc
    if not isinstance(data, dict):
        raise MetaCDNError(
            f"metadata response não é objeto: type={type(data).__name__}"
        )

    signed_url = data.get("url")
    if not signed_url:
        raise MetaCDNError(f"metadata response sem 'url': keys={list(data.keys())}")

    mime_type = data.get("mime_type", "") or ""
    try:
        file_size = int(data.get("file_size", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise MetaCDNError(
            f"metadata response file_size inválido: {data.get('file_size')!r}"
        ) from exc

    if file_size > _MAX_BYTES:
        raise MetaCDNError(
            f"file_size {file_size} excede cap {_MAX_BYTES} (Meta diz {mime_type})"
        )

    return signed_url, mime_type, file_size


async def download_signed_url(signed_url: str, token: Optional[str] = None) -> bytes:
    """
    Step 2: baixa bytes da URL assinada Meta CDN.

    Args:
        signed_url: vinda do step 1 (`fetch_media_url`). Expira em ~5min.
        token: WA_TOKEN (Bearer auth obrigatória mesmo na URL assinada).

    Returns:
        bytes do arquivo.

    Raises:
        MetaCDNError: timeout/network/4xx/5xx OR bytes excedem _MAX_BYTES.
    """
    token = token or getattr(env, "WA_TOKEN", None)
    if not token:
        raise MetaCDNError("WA_TOKEN não configurado")

    async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT_S) as client:
        try:
            resp = await client.get(
                signed_url,
                headers={"Authorization": f"Bearer {token}"},
                follow_redirects=True,
            )
        except httpx.TimeoutException as exc:
            raise MetaCDNError(f"timeout no download ({_DOWNLOAD_TIMEOUT_S}s)") from exc
        except httpx.HTTPError as exc:
            raise MetaCDNError(f"http error no download: {exc}") from exc

    if resp.status_code != 200:
        raise MetaCDNError(f"download retornou {resp.status_code}")

    blob = resp.content
    if len(blob) > _MAX_BYTES:
        raise MetaCDNError(f"bytes baixados {len(blob)} excedem cap {_MAX_BYTES}")

    return blob


async def download_meta_media(
    meta_media_id: str,
    token: Optional[str] = None,
) -> Tuple[bytes, str]:
    """
    Wrapper 2-step (metadata + download). Caller comum.

    Returns:
        (bytes, mime_type)
    """
    signed_url, mime_type, _file_size = await fetch_media_url(
        meta_media_id, token=token
    )
    logger.info(
        "meta_cdn_metadata_ok",
        media_id=meta_media_id,
        mime_type=mime_type,
        file_size=_file_size,
    )
    blob = await download_signed_url(signed_url, token=token)
    logger.info(
        "meta_cdn_download_ok",
        media_id=meta_media_id,
        bytes=len(blob),
    )
    return blob, mime_type
