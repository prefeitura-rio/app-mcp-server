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
from typing import Any, Dict, Optional, Set, Union

import httpx
from loguru import logger

from src.utils.error_interceptor import send_api_error


# Status codes que devem ser interceptados por padrão
DEFAULT_ERROR_STATUS_CODES: Set[int] = {400, 401, 403, 404, 500, 502, 503, 504}


class InterceptedHTTPClient:
    """
    Cliente HTTP que automaticamente envia erros para o interceptor.

    Suporta modo async (padrão) e sync via parâmetro.

    Args:
        user_id: ID do usuário (WhatsApp number) para tracking
        source: Dicionário identificando a origem do erro
        sync: Se True, usa modo síncrono. Padrão: False (async)
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
        **httpx_kwargs,
    ):
        self.user_id = user_id
        self.source = source
        self.sync = sync
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
    async def _intercept_error_async(
        self,
        url: str,
        request_body: Any,
        status_code: int,
        error_message: str,
        traceback_str: Optional[str] = None,
    ) -> None:
        """Envia erro para o interceptor de forma assíncrona."""
        await send_api_error(
            user_id=self.user_id,
            source=self.source,
            api_endpoint=str(url),
            request_body=request_body,
            status_code=status_code,
            error_message=error_message,
            traceback=traceback_str,
        )

    def _intercept_error_sync(
        self,
        url: str,
        request_body: Any,
        status_code: int,
        error_message: str,
        traceback_str: Optional[str] = None,
    ) -> None:
        """Envia erro para o interceptor de forma síncrona."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(send_api_error(
                user_id=self.user_id,
                source=self.source,
                api_endpoint=str(url),
                request_body=request_body,
                status_code=status_code,
                error_message=error_message,
                traceback=traceback_str,
            ))
        except RuntimeError:
            try:
                asyncio.run(send_api_error(
                    user_id=self.user_id,
                    source=self.source,
                    api_endpoint=str(url),
                    request_body=request_body,
                    status_code=status_code,
                    error_message=error_message,
                    traceback=traceback_str,
                ))
            except Exception as e:
                logger.warning(f"Falha ao enviar erro para interceptor: {e}")

    # --- Request methods ---
    async def request(
        self,
        method: str,
        url: str,
        *,
        intercept_errors: bool = True,
        error_status_codes: Optional[Set[int]] = None,
        **kwargs,
    ) -> httpx.Response:
        """
        Faz uma requisição HTTP async com interceptação automática de erros.
        """
        if self.sync:
            raise RuntimeError("Use request_sync() para modo sync")

        if error_status_codes is None:
            error_status_codes = DEFAULT_ERROR_STATUS_CODES

        request_body = kwargs.get("json") or kwargs.get("data") or kwargs.get("params")

        try:
            response = await self._client.request(method, url, **kwargs)

            if intercept_errors and response.status_code in error_status_codes:
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
        error_status_codes: Optional[Set[int]] = None,
        **kwargs,
    ) -> httpx.Response:
        """
        Faz uma requisição HTTP sync com interceptação automática de erros.
        """
        if not self.sync:
            raise RuntimeError("Use request() para modo async")

        if error_status_codes is None:
            error_status_codes = DEFAULT_ERROR_STATUS_CODES

        request_body = kwargs.get("json") or kwargs.get("data") or kwargs.get("params")

        try:
            response = self._client.request(method, url, **kwargs)

            if intercept_errors and response.status_code in error_status_codes:
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
