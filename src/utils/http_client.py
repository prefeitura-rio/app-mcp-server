"""
HTTP Client Wrapper com Interceptação Automática de Erros

Este módulo fornece wrapper para httpx que automaticamente
envia erros para o sistema de monitoramento via error interceptor.

Classes:
    InterceptedHTTPClient: Wrapper para httpx (async por padrão, sync opcional)

Uso async (padrão):
    async with InterceptedHTTPClient(
        user_id="5521999999999",
        source={"source": "mcp", "tool": "search"}
    ) as client:
        response = await client.get(url, params=params)

Uso sync:
    with InterceptedHTTPClient(
        user_id="5521999999999",
        source={"source": "mcp", "tool": "geocoding"},
        sync=True
    ) as client:
        response = client.get(url, params=params)
"""

import asyncio
import traceback as tb
from typing import AbstractSet, Any, Dict, FrozenSet, Optional, Sequence, Union

import httpx
from loguru import logger

from src.utils.error_interceptor import send_api_error
from src.utils.pii import (
    CHAVES_CREDENCIAL,
    MARCADOR_CREDENCIAL,
    redigir_credenciais,
    redigir_padroes_pii,
    resumir_texto_livre,
)


# Status codes que devem ser interceptados por padrão
DEFAULT_ERROR_STATUS_CODES: FrozenSet[int] = frozenset(
    {400, 401, 403, 404, 500, 502, 503, 504}
)

# Nomes de parâmetros/campos cujo valor nunca deve sair daqui em claro. A lista
# vive em `src/utils/pii.py` desde o CHATR-167, junto com os padrões usados pela
# barreira de log -- antes deste ticket havia uma cópia aqui e outra no
# `error_interceptor`, e nenhuma das duas cobria a saída de `logger.*`.
#
# É um alias, não uma cópia: os três módulos apontam para o mesmo objeto, e é
# por isso que ele é `frozenset` -- um `.add()` distraído aqui mudaria também a
# barreira de log.
#
# O que o CHATR-153 tinha acrescentado neste bloco foi junto: `x-goog-signature`
# está na lista lá, e as notas sobre a âncora `(?<!\w)`, o `re.escape` e o
# `sorted` determinístico vivem em `_CREDENCIAL_EM_QUERY`, que é onde o padrão
# passou a ser compilado.
SENSITIVE_KEYS: FrozenSet[str] = CHAVES_CREDENCIAL


def redact_text(texto: Optional[str]) -> Optional[str]:
    """
    Redige credencial e PII embutidas em texto livre.

    Várias APIs (IPTU, entre outras) autenticam por query string, e exceções do
    httpx como TooManyRedirects ou InvalidURL embutem a URL completa na
    mensagem. Sem isso, a credencial acabaria no sistema de monitoramento.

    Desde o CHATR-167 também passa pelos padrões de PII: `error_message` e
    `traceback` chegam aqui a caminho do interceptor e carregam com frequência o
    CPF ou o telefone que originou a chamada.
    """
    if not texto:
        return texto
    return redigir_padroes_pii(redigir_credenciais(texto))


def redact_body(body: Any) -> Any:
    """Redige campos sensíveis de um body antes de reportá-lo ao interceptor."""
    if isinstance(body, dict):
        return {
            chave: (
                MARCADOR_CREDENCIAL
                if str(chave).lower() in SENSITIVE_KEYS
                else redact_body(valor)
            )
            for chave, valor in body.items()
        }
    if isinstance(body, (list, tuple)):
        return [redact_body(item) for item in body]
    if isinstance(body, str):
        return redact_text(body)
    return body


def _summarize_free_text_fields(body: Any, free_text_fields: AbstractSet[str]) -> Any:
    """Troca, em qualquer nível de dict ou lista, os campos nomeados pelo resumo."""
    if isinstance(body, dict):
        return {
            chave: (
                resumir_texto_livre(valor)
                if str(chave).lower() in free_text_fields
                else _summarize_free_text_fields(valor, free_text_fields)
            )
            for chave, valor in body.items()
        }
    if isinstance(body, (list, tuple)):
        return [_summarize_free_text_fields(item, free_text_fields) for item in body]
    return body


def summarize_free_text(body: Any, free_text_fields: AbstractSet[str]) -> Any:
    """
    Troca os campos de texto livre do cidadão por `resumir_texto_livre`.

    O campo vira tamanho e tipos de dado pessoal, nunca o texto (DEV-279): a
    redação por padrão de `redact_body` tira CPF e telefone, mas o resto da
    pergunta (doença, família, dívida) não se redige. Os nomes (já em
    minúsculas) casam sem diferenciar maiúsculas, em qualquer nível do corpo.
    Corpo que não é dict nem lista (texto ou bytes crus) não tem onde achar o
    campo: com campos declarados, vai inteiro como resumo.
    """
    if not free_text_fields or body is None:
        return body
    if isinstance(body, (str, bytes, bytearray)):
        return resumir_texto_livre(body)
    return _summarize_free_text_fields(body, free_text_fields)


def raise_for_status_except(
    response: httpx.Response, skip_codes: AbstractSet[int]
) -> None:
    """Chama raise_for_status() ignorando status codes que representam estados válidos.

    Use sempre que um código 4xx for semanticamente um "resultado vazio" e não uma
    falha — caso contrário o FastMCP captura a exceção e retorna isError=True ao
    cliente, que levanta ToolException e quebra o turno.

    Exemplo:
        raise_for_status_except(response, skip_codes={404})  # 404 nunca levanta
    """
    if response.status_code not in skip_codes:
        response.raise_for_status()


class InterceptedHTTPClient:
    """
    Cliente HTTP que automaticamente envia erros para o interceptor.

    Suporta modo async (padrão) e sync via parâmetro.

    Args:
        user_id: ID do usuário (WhatsApp number) para tracking
        source: Dicionário identificando a origem do erro
        sync: Se True, usa modo síncrono. Padrão: False (async)
        free_text_fields: Nomes dos campos do corpo (json, data ou params) com
            texto livre do cidadão. No report ao interceptor viram tamanho e
            tipos de dado pessoal (`resumir_texto_livre`), nunca o texto. A
            requisição de saída não muda (DEV-279).
        **httpx_kwargs: Argumentos passados para httpx.Client/AsyncClient

    Example - Async (padrão):
        >>> async with InterceptedHTTPClient(
        ...     user_id="5521999999999",
        ...     source={"source": "mcp", "tool": "search"},
        ...     timeout=30.0,
        ... ) as client:
        ...     response = await client.get(url, params={"q": "test"})

    Example - Sync:
        >>> with InterceptedHTTPClient(
        ...     user_id="5521999999999",
        ...     source={"source": "mcp", "tool": "geocoding"},
        ...     sync=True,
        ...     timeout=10.0,
        ... ) as client:
        ...     response = client.get(url, params={"q": "test"})
    """

    def __init__(
        self,
        user_id: str,
        source: Dict[str, Any],
        sync: bool = False,
        free_text_fields: Sequence[str] = (),
        **httpx_kwargs,
    ):
        self.user_id = user_id
        self.source = source
        self.sync = sync
        if isinstance(free_text_fields, str):
            # Uma string sozinha é um nome, não uma sequência de letras.
            free_text_fields = (free_text_fields,)
        self.free_text_fields: FrozenSet[str] = frozenset(
            str(campo).lower() for campo in free_text_fields
        )
        self.httpx_kwargs = httpx_kwargs
        self._client: Optional[Union[httpx.Client, httpx.AsyncClient]] = None

    # --- Async context manager ---
    async def __aenter__(self) -> "InterceptedHTTPClient":
        if self.sync:
            raise RuntimeError("Use 'with' para modo sync, não 'async with'")
        self._client = httpx.AsyncClient(**self.httpx_kwargs)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client and isinstance(self._client, httpx.AsyncClient):
            await self._client.aclose()

    # --- Sync context manager ---
    def __enter__(self) -> "InterceptedHTTPClient":
        if not self.sync:
            raise RuntimeError("Use 'async with' para modo async, ou passe sync=True")
        self._client = httpx.Client(**self.httpx_kwargs)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._client and isinstance(self._client, httpx.Client):
            self._client.close()

    # --- Error interception ---
    def _report_body(self, request_body: Any) -> Any:
        """Corpo como vai ao interceptor: sem texto livre e sem credenciais."""
        return redact_body(summarize_free_text(request_body, self.free_text_fields))

    async def _intercept_error_async(
        self,
        url: str,
        request_body: Any,
        status_code: int,
        error_message: str,
        traceback_str: Optional[str] = None,
    ) -> None:
        """Envia erro para o interceptor de forma assíncrona, sem credenciais."""
        await send_api_error(
            user_id=self.user_id,
            source=self.source,
            api_endpoint=redact_text(str(url)),
            request_body=self._report_body(request_body),
            status_code=status_code,
            error_message=redact_text(error_message),
            traceback=redact_text(traceback_str),
        )

    def _intercept_error_sync(
        self,
        url: str,
        request_body: Any,
        status_code: int,
        error_message: str,
        traceback_str: Optional[str] = None,
    ) -> None:
        """Envia erro para o interceptor de forma síncrona, sem credenciais."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                send_api_error(
                    user_id=self.user_id,
                    source=self.source,
                    api_endpoint=redact_text(str(url)),
                    request_body=self._report_body(request_body),
                    status_code=status_code,
                    error_message=redact_text(error_message),
                    traceback=redact_text(traceback_str),
                )
            )
        except RuntimeError:
            try:
                asyncio.run(
                    send_api_error(
                        user_id=self.user_id,
                        source=self.source,
                        api_endpoint=redact_text(str(url)),
                        request_body=self._report_body(request_body),
                        status_code=status_code,
                        error_message=redact_text(error_message),
                        traceback=redact_text(traceback_str),
                    )
                )
            except Exception as e:
                logger.warning(f"Falha ao enviar erro para interceptor: {e}")

    # --- Request methods ---
    async def request(
        self,
        method: str,
        url: str,
        *,
        intercept_errors: bool = True,
        error_status_codes: Optional[AbstractSet[int]] = None,
        **kwargs,
    ) -> httpx.Response:
        """
        Faz uma requisição HTTP async com interceptação automática de erros.

        HTTP status errors (4xx/5xx) are only intercepted when error_status_codes is
        explicitly provided. By default, status codes are not intercepted so that
        the @interceptor decorator (via raise_for_status()) is the single source of
        truth for reporting HTTP errors and avoiding duplicate alerts.
        """
        if self.sync:
            raise RuntimeError("Use request_sync() para modo sync")

        if not isinstance(self._client, httpx.AsyncClient):
            raise RuntimeError(
                "InterceptedHTTPClient não foi inicializado via context manager. "
                "Use 'async with InterceptedHTTPClient(...) as client:' para modo async."
            )

        request_body = kwargs.get("json") or kwargs.get("data") or kwargs.get("params")

        try:
            response = await self._client.request(method, url, **kwargs)

            if (
                intercept_errors
                and error_status_codes
                and response.status_code in error_status_codes
            ):
                try:
                    response_text = response.text[:500] if response.text else ""
                except Exception:
                    response_text = ""

                await self._intercept_error_async(
                    url=url,
                    request_body=request_body,
                    status_code=response.status_code,
                    error_message=f"HTTP {response.status_code}: {response_text}",
                )

            return response

        except httpx.TimeoutException:
            if intercept_errors:
                await self._intercept_error_async(
                    url=url,
                    request_body=request_body,
                    status_code=408,
                    error_message="Request timeout",
                    traceback_str=tb.format_exc(),
                )
            raise

        except httpx.ConnectError as e:
            if intercept_errors:
                await self._intercept_error_async(
                    url=url,
                    request_body=request_body,
                    status_code=0,
                    error_message=f"Connection error: {str(e)}",
                    traceback_str=tb.format_exc(),
                )
            raise

        except Exception as e:
            if intercept_errors:
                await self._intercept_error_async(
                    url=url,
                    request_body=request_body,
                    status_code=0,
                    error_message=str(e),
                    traceback_str=tb.format_exc(),
                )
            raise

    def request_sync(
        self,
        method: str,
        url: str,
        *,
        intercept_errors: bool = True,
        error_status_codes: Optional[AbstractSet[int]] = None,
        **kwargs,
    ) -> httpx.Response:
        """
        Faz uma requisição HTTP sync com interceptação automática de erros.

        HTTP status errors (4xx/5xx) are only intercepted when error_status_codes is
        explicitly provided. By default, status codes are not intercepted so that
        the @interceptor decorator (via raise_for_status()) is the single source of
        truth for reporting HTTP errors and avoiding duplicate alerts.
        """
        if not self.sync:
            raise RuntimeError("Use request() para modo async")

        if not isinstance(self._client, httpx.Client):
            raise RuntimeError(
                "InterceptedHTTPClient não foi inicializado via context manager. "
                "Use 'with InterceptedHTTPClient(..., sync=True) as client:' para modo sync."
            )

        request_body = kwargs.get("json") or kwargs.get("data") or kwargs.get("params")

        try:
            response = self._client.request(method, url, **kwargs)

            if (
                intercept_errors
                and error_status_codes
                and response.status_code in error_status_codes
            ):
                try:
                    response_text = response.text[:500] if response.text else ""
                except Exception:
                    response_text = ""

                self._intercept_error_sync(
                    url=url,
                    request_body=request_body,
                    status_code=response.status_code,
                    error_message=f"HTTP {response.status_code}: {response_text}",
                )

            return response

        except httpx.TimeoutException:
            if intercept_errors:
                self._intercept_error_sync(
                    url=url,
                    request_body=request_body,
                    status_code=408,
                    error_message="Request timeout",
                    traceback_str=tb.format_exc(),
                )
            raise

        except httpx.ConnectError as e:
            if intercept_errors:
                self._intercept_error_sync(
                    url=url,
                    request_body=request_body,
                    status_code=0,
                    error_message=f"Connection error: {str(e)}",
                    traceback_str=tb.format_exc(),
                )
            raise

        except Exception as e:
            if intercept_errors:
                self._intercept_error_sync(
                    url=url,
                    request_body=request_body,
                    status_code=0,
                    error_message=str(e),
                    traceback_str=tb.format_exc(),
                )
            raise

    # --- Async convenience methods ---
    async def get(self, url: str, **kwargs) -> httpx.Response:
        """GET request async."""
        if self.sync:
            return self.request_sync("GET", url, **kwargs)
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs) -> httpx.Response:
        """POST request async."""
        if self.sync:
            return self.request_sync("POST", url, **kwargs)
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs) -> httpx.Response:
        """PUT request async."""
        if self.sync:
            return self.request_sync("PUT", url, **kwargs)
        return await self.request("PUT", url, **kwargs)

    async def delete(self, url: str, **kwargs) -> httpx.Response:
        """DELETE request async."""
        if self.sync:
            return self.request_sync("DELETE", url, **kwargs)
        return await self.request("DELETE", url, **kwargs)

    async def patch(self, url: str, **kwargs) -> httpx.Response:
        """PATCH request async."""
        if self.sync:
            return self.request_sync("PATCH", url, **kwargs)
        return await self.request("PATCH", url, **kwargs)

    async def head(self, url: str, **kwargs) -> httpx.Response:
        """HEAD request async."""
        if self.sync:
            return self.request_sync("HEAD", url, **kwargs)
        return await self.request("HEAD", url, **kwargs)

    # --- Sync convenience methods ---
    def get_sync(self, url: str, **kwargs) -> httpx.Response:
        """GET request sync."""
        return self.request_sync("GET", url, **kwargs)

    def post_sync(self, url: str, **kwargs) -> httpx.Response:
        """POST request sync."""
        return self.request_sync("POST", url, **kwargs)

    def put_sync(self, url: str, **kwargs) -> httpx.Response:
        """PUT request sync."""
        return self.request_sync("PUT", url, **kwargs)

    def delete_sync(self, url: str, **kwargs) -> httpx.Response:
        """DELETE request sync."""
        return self.request_sync("DELETE", url, **kwargs)

    def patch_sync(self, url: str, **kwargs) -> httpx.Response:
        """PATCH request sync."""
        return self.request_sync("PATCH", url, **kwargs)

    def head_sync(self, url: str, **kwargs) -> httpx.Response:
        """HEAD request sync."""
        return self.request_sync("HEAD", url, **kwargs)
