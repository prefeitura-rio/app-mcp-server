"""
HTTP Client Wrappers com Interceptação Automática de Erros

Este módulo fornece wrappers para httpx e aiohttp que automaticamente
enviam erros para o sistema de monitoramento via error interceptor.

Classes:
    InterceptedHTTPClient: Wrapper async para httpx
    InterceptedAioHTTPClient: Wrapper async para aiohttp

Uso:
    async with InterceptedHTTPClient(
        user_id="5521999999999",
        source={"source": "mcp", "tool": "search"}
    ) as client:
        response = await client.get(url, params=params)
        # Erros são automaticamente interceptados!
"""

import traceback as tb
from typing import Any, Dict, Optional, Set

import aiohttp
import httpx

from src.utils.error_interceptor import send_api_error


# Status codes que devem ser interceptados por padrão
DEFAULT_ERROR_STATUS_CODES: Set[int] = {400, 401, 403, 404, 500, 502, 503, 504}


class InterceptedHTTPClient:
    """
    Cliente HTTP async (httpx) que automaticamente envia erros para o interceptor.

    Substitui httpx.AsyncClient com interceptação transparente de erros.
    Mantém a mesma API do httpx para facilitar migração.

    Args:
        user_id: ID do usuário (WhatsApp number) para tracking
        source: Dicionário com qualquer formato identificando a origem do erro
        **httpx_kwargs: Argumentos passados para httpx.AsyncClient (timeout, proxy, etc.)

    Example - Tool simples:
        >>> async with InterceptedHTTPClient(
        ...     user_id="5521999999999",
        ...     source={"source": "mcp", "tool": "search"},
        ...     timeout=30.0,
        ... ) as client:
        ...     response = await client.get(url, params={"q": "test"})
        # Flowname em caso de erro: "source=mcp | tool=search"

    Example - Workflow:
        >>> async with InterceptedHTTPClient(
        ...     user_id="5521999999999",
        ...     source={
        ...         "source": "mcp",
        ...         "tool": "multi_step_service",
        ...         "workflow": "iptu_pagamento",
        ...         "step": "consultar_guias"
        ...     },
        ...     timeout=30.0,
        ... ) as client:
        ...     response = await client.get(url, params={"inscricao": "123"})
        # Flowname em caso de erro: "source=mcp | tool=multi_step_service | workflow=iptu_pagamento | step=consultar_guias"
    """

    def __init__(
        self,
        user_id: str,
        source: Dict[str, Any],
        **httpx_kwargs,
    ):
        self.user_id = user_id
        self.source = source
        self.httpx_kwargs = httpx_kwargs
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "InterceptedHTTPClient":
        self._client = httpx.AsyncClient(**self.httpx_kwargs)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.aclose()

    async def _intercept_error(
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
        Faz uma requisição HTTP com interceptação automática de erros.

        Args:
            method: Método HTTP (GET, POST, PUT, DELETE, etc.)
            url: URL do endpoint
            intercept_errors: Se True, intercepta erros automaticamente (padrão: True)
            error_status_codes: Status codes a interceptar (padrão: 400, 401, 403, 404, 500, 502, 503, 504)
            **kwargs: Argumentos passados para httpx (params, json, data, headers, etc.)

        Returns:
            httpx.Response: Resposta da requisição

        Raises:
            httpx.TimeoutException: Se timeout ocorrer (interceptado antes de re-raise)
            Exception: Qualquer outra exceção (interceptada antes de re-raise)
        """
        if error_status_codes is None:
            error_status_codes = DEFAULT_ERROR_STATUS_CODES

        # Extrai request body para logging
        request_body = kwargs.get("json") or kwargs.get("data") or kwargs.get("params")

        try:
            response = await self._client.request(method, url, **kwargs)

            # Intercepta erros de status code
            if intercept_errors and response.status_code in error_status_codes:
                # Tenta ler o texto da resposta de forma segura
                try:
                    response_text = response.text[:500] if response.text else ""
                except Exception:
                    response_text = ""

                await self._intercept_error(
                    url=url,
                    request_body=request_body,
                    status_code=response.status_code,
                    error_message=f"HTTP {response.status_code}: {response_text}",
                )

            return response

        except httpx.TimeoutException:
            if intercept_errors:
                await self._intercept_error(
                    url=url,
                    request_body=request_body,
                    status_code=408,
                    error_message="Request timeout",
                    traceback_str=tb.format_exc(),
                )
            raise

        except httpx.ConnectError as e:
            if intercept_errors:
                await self._intercept_error(
                    url=url,
                    request_body=request_body,
                    status_code=0,
                    error_message=f"Connection error: {str(e)}",
                    traceback_str=tb.format_exc(),
                )
            raise

        except Exception as e:
            if intercept_errors:
                await self._intercept_error(
                    url=url,
                    request_body=request_body,
                    status_code=0,
                    error_message=str(e),
                    traceback_str=tb.format_exc(),
                )
            raise

    async def get(self, url: str, **kwargs) -> httpx.Response:
        """GET request com interceptação."""
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs) -> httpx.Response:
        """POST request com interceptação."""
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs) -> httpx.Response:
        """PUT request com interceptação."""
        return await self.request("PUT", url, **kwargs)

    async def delete(self, url: str, **kwargs) -> httpx.Response:
        """DELETE request com interceptação."""
        return await self.request("DELETE", url, **kwargs)

    async def patch(self, url: str, **kwargs) -> httpx.Response:
        """PATCH request com interceptação."""
        return await self.request("PATCH", url, **kwargs)

    async def head(self, url: str, **kwargs) -> httpx.Response:
        """HEAD request com interceptação."""
        return await self.request("HEAD", url, **kwargs)


class InterceptedAioHTTPClient:
    """
    Cliente HTTP async (aiohttp) que automaticamente envia erros para o interceptor.

    Substitui aiohttp.ClientSession com interceptação transparente de erros.
    API similar ao InterceptedHTTPClient para consistência.

    Args:
        user_id: ID do usuário (WhatsApp number) para tracking
        source: Dicionário com qualquer formato identificando a origem do erro
        **session_kwargs: Argumentos passados para aiohttp.ClientSession

    Example - Tool simples:
        >>> async with InterceptedAioHTTPClient(
        ...     user_id="5521999999999",
        ...     source={"source": "mcp", "tool": "memory"},
        ...     timeout=aiohttp.ClientTimeout(total=30)
        ... ) as client:
        ...     response = await client.get(url, headers=headers)
        ...     data = await response.json()
        # Flowname em caso de erro: "source=mcp | tool=memory"

    Example - Workflow:
        >>> async with InterceptedAioHTTPClient(
        ...     user_id="5521999999999",
        ...     source={
        ...         "source": "mcp",
        ...         "tool": "multi_step_service",
        ...         "workflow": "poda_de_arvore",
        ...         "step": "get_user_info"
        ...     },
        ...     timeout=aiohttp.ClientTimeout(total=30)
        ... ) as client:
        ...     response = await client.get(url, headers=headers)
        ...     data = await response.json()
        # Flowname em caso de erro: "source=mcp | tool=multi_step_service | workflow=poda_de_arvore | step=get_user_info"
    """

    def __init__(
        self,
        user_id: str,
        source: Dict[str, Any],
        **session_kwargs,
    ):
        self.user_id = user_id
        self.source = source
        self.session_kwargs = session_kwargs
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "InterceptedAioHTTPClient":
        self._session = aiohttp.ClientSession(**self.session_kwargs)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._session:
            await self._session.close()

    async def _intercept_error(
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

    async def request(
        self,
        method: str,
        url: str,
        *,
        intercept_errors: bool = True,
        error_status_codes: Optional[Set[int]] = None,
        **kwargs,
    ) -> aiohttp.ClientResponse:
        """
        Faz uma requisição HTTP com interceptação automática de erros.

        Args:
            method: Método HTTP (GET, POST, PUT, DELETE, etc.)
            url: URL do endpoint
            intercept_errors: Se True, intercepta erros automaticamente
            error_status_codes: Status codes a interceptar
            **kwargs: Argumentos passados para aiohttp (data, json, headers, etc.)

        Returns:
            aiohttp.ClientResponse: Resposta da requisição

        Note:
            A resposta NÃO é automaticamente lida. Você precisa chamar
            response.json(), response.text(), etc.
        """
        if error_status_codes is None:
            error_status_codes = DEFAULT_ERROR_STATUS_CODES

        # Extrai request body para logging
        request_body = kwargs.get("json") or kwargs.get("data")

        try:
            response = await self._session.request(method, url, **kwargs)

            # Intercepta erros de status code
            if intercept_errors and response.status in error_status_codes:
                # Tenta ler o texto da resposta de forma segura
                try:
                    response_text = await response.text()
                    response_text = response_text[:500] if response_text else ""
                except Exception:
                    response_text = ""

                await self._intercept_error(
                    url=url,
                    request_body=request_body,
                    status_code=response.status,
                    error_message=f"HTTP {response.status}: {response_text}",
                )

            return response

        except aiohttp.ServerTimeoutError:
            if intercept_errors:
                await self._intercept_error(
                    url=url,
                    request_body=request_body,
                    status_code=408,
                    error_message="Request timeout",
                    traceback_str=tb.format_exc(),
                )
            raise

        except aiohttp.ClientConnectorError as e:
            if intercept_errors:
                await self._intercept_error(
                    url=url,
                    request_body=request_body,
                    status_code=0,
                    error_message=f"Connection error: {str(e)}",
                    traceback_str=tb.format_exc(),
                )
            raise

        except aiohttp.ClientResponseError as e:
            if intercept_errors:
                await self._intercept_error(
                    url=url,
                    request_body=request_body,
                    status_code=e.status,
                    error_message=str(e),
                    traceback_str=tb.format_exc(),
                )
            raise

        except Exception as e:
            if intercept_errors:
                await self._intercept_error(
                    url=url,
                    request_body=request_body,
                    status_code=0,
                    error_message=str(e),
                    traceback_str=tb.format_exc(),
                )
            raise

    async def get(self, url: str, **kwargs) -> aiohttp.ClientResponse:
        """GET request com interceptação."""
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs) -> aiohttp.ClientResponse:
        """POST request com interceptação."""
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs) -> aiohttp.ClientResponse:
        """PUT request com interceptação."""
        return await self.request("PUT", url, **kwargs)

    async def delete(self, url: str, **kwargs) -> aiohttp.ClientResponse:
        """DELETE request com interceptação."""
        return await self.request("DELETE", url, **kwargs)

    async def patch(self, url: str, **kwargs) -> aiohttp.ClientResponse:
        """PATCH request com interceptação."""
        return await self.request("PATCH", url, **kwargs)

    async def head(self, url: str, **kwargs) -> aiohttp.ClientResponse:
        """HEAD request com interceptação."""
        return await self.request("HEAD", url, **kwargs)
