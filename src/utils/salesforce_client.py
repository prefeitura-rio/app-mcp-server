"""
Cliente Salesforce minimal pro MCP tools que precisam baixar bytes de
``ContentVersion`` (ex: ``analyze_inbound_image`` do Gemini Vision).

Estratégia:
* OAuth 2.0 Client Credentials Flow (sem user nem refresh token). Reusa a
  Connected App "MuleSoft LangGraph Integration" que já existe em devwilliam
  para o Mule outbound (mesmo client_id/secret no Infisical).
* Cache do access_token em memória do processo. SF token TTL é tipicamente
  2-12h dependendo da org policy; usamos 25min defensivo + Bearer 401 force
  refresh on demand.
* Single GET request por download (sem chunking — ContentVersion ≤ ~16 MB
  cabe em memória sem stress).

Dependências:
* ``SALESFORCE_INSTANCE_URL`` (ex: ``https://prefeitura-rio--devwilliam.sandbox.my.salesforce.com``)
* ``SALESFORCE_CLIENT_ID`` (Connected App)
* ``SALESFORCE_CLIENT_SECRET``

Quando qualquer um faltar, :func:`download_content_version` retorna ``None``
e o caller (analyze_inbound_image) cai num fallback graceful. Setup do
Infisical em produção fica como ação operacional separada.

Segurança:
* download_path **NÃO** é controlado pelo cliente do MCP — vem do prefix
  ``[INBOUND_MEDIA]`` que o Engine compõe a partir do payload do Mule, que
  por sua vez vem do Apex. Risco de SSRF baixo, mas validamos defensivamente
  que o path começa com ``/services/data/`` antes do fetch.
"""

from __future__ import annotations

import re
import time
from typing import Optional

import httpx

from src.config import env
from src.utils.log import logger


_TOKEN_TTL_SECONDS = 25 * 60  # 25 min — refresh proativo antes do SF default ~30min
_DOWNLOAD_TIMEOUT_SECONDS = 30
_MAX_BYTES = 20 * 1024 * 1024  # 20 MB — alinhado com Gemini inline_data limit

# Whitelist estrita: aceita SOMENTE o endpoint VersionData de ContentVersion.
# Forma:
#   /services/data/v<MAJOR>.<MINOR>/sobjects/ContentVersion/<15- ou 18-char Id>/VersionData
# Tudo o mais (SOQL ``/query/``, outros sObjects, search, identity API, etc.)
# fica rejeitado em _is_safe_download_path.
_CONTENT_VERSION_VERSION_DATA_PATTERN = re.compile(
    r"^/services/data/v\d+\.\d+/sobjects/ContentVersion/[A-Za-z0-9]{15,18}/VersionData$"
)


# Cache simples in-memory. Single-process MCP server; pra escalar pra
# multi-replica, mover pra Redis ou similar.
_cached_token: Optional[str] = None
_cached_token_at: float = 0.0


def _config_ready() -> bool:
    instance = getattr(env, "SALESFORCE_INSTANCE_URL", None)
    cid = getattr(env, "SALESFORCE_CLIENT_ID", None)
    sec = getattr(env, "SALESFORCE_CLIENT_SECRET", None)
    return bool(instance and cid and sec)


def _fetch_access_token() -> Optional[str]:
    """OAuth Client Credentials. Retorna access_token ou None em falha."""
    if not _config_ready():
        logger.warning(
            "salesforce_client: faltam env vars "
            "SALESFORCE_INSTANCE_URL/CLIENT_ID/CLIENT_SECRET; pulando download."
        )
        return None
    url = f"{env.SALESFORCE_INSTANCE_URL.rstrip('/')}/services/oauth2/token"
    try:
        with httpx.Client(timeout=15) as client:
            r = client.post(
                url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": env.SALESFORCE_CLIENT_ID,
                    "client_secret": env.SALESFORCE_CLIENT_SECRET,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except httpx.HTTPError as e:
        logger.error(f"salesforce_client: OAuth request falhou: {e}")
        return None
    if r.status_code != 200:
        # Não logamos o body completo — pode conter parte do secret em error
        logger.error(
            f"salesforce_client: OAuth retornou {r.status_code}; "
            f"first 120 chars: {r.text[:120]!r}"
        )
        return None
    try:
        token = r.json().get("access_token")
    except ValueError:
        logger.error("salesforce_client: OAuth response não é JSON válido")
        return None
    if not token:
        logger.error("salesforce_client: OAuth response sem access_token")
        return None
    return token


def _get_access_token(force_refresh: bool = False) -> Optional[str]:
    global _cached_token, _cached_token_at
    if (
        not force_refresh
        and _cached_token
        and (time.time() - _cached_token_at) < _TOKEN_TTL_SECONDS
    ):
        return _cached_token
    token = _fetch_access_token()
    if token:
        _cached_token = token
        _cached_token_at = time.time()
    return token


def _is_safe_download_path(path: str) -> bool:
    """
    Aceita SOMENTE o endpoint ContentVersion VersionData. Restritivo por
    design: a tool é exposta via MCP, então um caller (incluindo LLM com
    prompt injection) poderia injetar caminhos arbitrários do REST como
    ``/services/data/v62.0/query/?q=...`` (SOQL) ou outros sObjects, o
    que daria à tool acesso a dados privilegiados da org. O regex aqui é
    o único discriminador entre "baixar arquivo opaco" e "consultar dados
    arbitrários".

    Forma aceita:
        ``/services/data/v<MAJOR>.<MINOR>/sobjects/ContentVersion/<ID>/VersionData``
    onde ``<ID>`` é o 15- ou 18-char Salesforce Id (alfanumérico).
    """
    if not isinstance(path, str):
        return False
    return bool(_CONTENT_VERSION_VERSION_DATA_PATTERN.fullmatch(path))


def download_content_version(download_path: str) -> Optional[bytes]:
    """
    Baixa bytes via Salesforce REST API usando OAuth Client Credentials.

    Args:
        download_path: caminho relativo no SF (ex:
            ``/services/data/v62.0/sobjects/ContentVersion/068xxx/VersionData``).
            Validado contra SSRF.

    Returns:
        Bytes do arquivo, ou ``None`` em qualquer falha (config ausente, auth,
        path inválido, HTTP error, tamanho excessivo). Caller deve tratar como
        "download não disponível, cair em fallback".
    """
    if not _is_safe_download_path(download_path):
        logger.warning(
            f"salesforce_client: download_path rejeitado por safety: {download_path!r}"
        )
        return None

    token = _get_access_token()
    if not token:
        return None

    url = f"{env.SALESFORCE_INSTANCE_URL.rstrip('/')}{download_path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/octet-stream"}

    body = _stream_with_limit(url, headers)
    # 401 = token possivelmente expirou; força refresh e tenta 1x mais.
    if isinstance(body, int) and body == 401:
        logger.info("salesforce_client: 401 do SF; refresh token e retry")
        token = _get_access_token(force_refresh=True)
        if not token:
            return None
        headers["Authorization"] = f"Bearer {token}"
        body = _stream_with_limit(url, headers)
    if isinstance(body, int) or body is None:
        # int → status code != 200 (já logado dentro de _stream_with_limit);
        # None → exception (já logado).
        return None
    return body


def _stream_with_limit(url, headers):
    """
    Download via streaming com enforcement do byte limit ANTES de bufferizar.

    Salesforce ContentVersion pode ter > 20 MB (limite teórico SF ~2 GB).
    Sem stream, ``httpx.Client.get`` buferiza response inteira na memória
    antes do check de tamanho, dando OOM no MCP server. Esta função:
      1. Inspeciona ``Content-Length`` header antes de iniciar o corpo.
      2. Lê em chunks acumulando byte counter; aborta se passar de _MAX_BYTES.

    Returns:
        bytes em sucesso, ``int`` (status code != 200), ou ``None`` (exception).
    """
    try:
        with httpx.Client(
            timeout=_DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True
        ) as c:
            with c.stream("GET", url, headers=headers) as resp:
                if resp.status_code != 200:
                    if resp.status_code != 401:
                        logger.error(
                            f"salesforce_client: download {url[-80:]!r} retornou "
                            f"{resp.status_code}"
                        )
                    return resp.status_code
                content_length = resp.headers.get("Content-Length")
                if content_length and content_length.isdigit():
                    if int(content_length) > _MAX_BYTES:
                        logger.warning(
                            f"salesforce_client: Content-Length "
                            f"{content_length} > limite {_MAX_BYTES}; abort"
                        )
                        return resp.status_code
                buf = bytearray()
                for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                    buf.extend(chunk)
                    if len(buf) > _MAX_BYTES:
                        logger.warning(
                            f"salesforce_client: download excedeu {_MAX_BYTES} "
                            f"bytes durante stream; abort"
                        )
                        return resp.status_code
                return bytes(buf)
    except httpx.HTTPError as e:
        logger.error(f"salesforce_client: download stream falhou: {e}")
        return None


async def download_content_version_async(download_path: str) -> Optional[bytes]:
    """
    Wrapper async pra :func:`download_content_version` que executa em thread
    pool. O cliente subjacente é ``httpx.Client`` síncrono — chamar direto
    de coroutine bloqueia o event loop do MCP server inteiro (FastMCP roda
    todas tools no mesmo loop). Usamos :func:`asyncio.to_thread` pra cumprir
    contrato async sem reescrever o cliente.

    Por que não ``httpx.AsyncClient``: cache de access_token + lógica de
    retry 401 + streaming check rodam em pipeline simples sync. Migrar tudo
    pra async dobraria a complexidade sem ganho real (chamada é
    request-response curta; concorrência alta no MCP é improvável neste
    POC). Mantemos sync internamente + offload.
    """
    import asyncio

    return await asyncio.to_thread(download_content_version, download_path)
