"""
Testes para InterceptedHTTPClient.

Estes testes verificam que o wrapper HTTP intercepta erros corretamente
usando mocks para simular respostas de erro sem fazer requisições reais.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx

from src.utils.http_client import (
    InterceptedHTTPClient,
    DEFAULT_ERROR_STATUS_CODES,
)


class TestInterceptedHTTPClient:
    """Testes para o wrapper httpx."""

    @pytest.mark.asyncio
    async def test_does_not_intercept_500_by_default(self):
        """Verifica que erros 500 NÃO são interceptados por padrão (evita duplicidade com @interceptor)."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        with patch(
            "src.utils.http_client.send_api_error", new_callable=AsyncMock
        ) as mock_send:
            async with InterceptedHTTPClient(
                user_id="5521999999999",
                source={"source": "mcp", "tool": "test"},
            ) as client:
                client._client = AsyncMock()
                client._client.request = AsyncMock(return_value=mock_response)

                response = await client.get("https://api.example.com/test")

                assert response.status_code == 500
                mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_intercepts_500_when_explicitly_configured(self):
        """Verifica que erros 500 SÃO interceptados quando error_status_codes é passado explicitamente."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        with patch(
            "src.utils.http_client.send_api_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            async with InterceptedHTTPClient(
                user_id="5521999999999",
                source={"source": "mcp", "tool": "test"},
            ) as client:
                client._client = AsyncMock()
                client._client.request = AsyncMock(return_value=mock_response)

                response = await client.get(
                    "https://api.example.com/test",
                    error_status_codes={500},
                )

                assert response.status_code == 500
                mock_send.assert_called_once()

                call_kwargs = mock_send.call_args[1]
                assert call_kwargs["user_id"] == "5521999999999"
                assert call_kwargs["source"]["source"] == "mcp"
                assert call_kwargs["source"]["tool"] == "test"
                assert call_kwargs["status_code"] == 500
                assert "https://api.example.com/test" in call_kwargs["api_endpoint"]

    @pytest.mark.asyncio
    async def test_does_not_intercept_404_by_default(self):
        """Verifica que erros 404 NÃO são interceptados por padrão."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Not Found"

        with patch(
            "src.utils.http_client.send_api_error", new_callable=AsyncMock
        ) as mock_send:
            async with InterceptedHTTPClient(
                user_id="5521888888888",
                source={"source": "mcp", "tool": "test"},
            ) as client:
                client._client = AsyncMock()
                client._client.request = AsyncMock(return_value=mock_response)

                response = await client.get("https://api.example.com/iptu/123")

                assert response.status_code == 404
                mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_intercepts_timeout(self):
        """Verifica interceptação de timeout."""
        with patch(
            "src.utils.http_client.send_api_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            async with InterceptedHTTPClient(
                user_id="5521777777777",
                source={"source": "mcp", "tool": "test"},
            ) as client:
                client._client = AsyncMock()
                client._client.request = AsyncMock(
                    side_effect=httpx.TimeoutException("Connection timeout")
                )

                with pytest.raises(httpx.TimeoutException):
                    await client.get("https://api.example.com/slow")

                mock_send.assert_called_once()
                call_kwargs = mock_send.call_args[1]
                assert call_kwargs["status_code"] == 408
                assert "timeout" in call_kwargs["error_message"].lower()

    @pytest.mark.asyncio
    async def test_intercepts_connection_error(self):
        """Verifica interceptação de erro de conexão."""
        with patch(
            "src.utils.http_client.send_api_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            async with InterceptedHTTPClient(
                user_id="5521666666666",
                source={"source": "mcp", "tool": "test"},
            ) as client:
                client._client = AsyncMock()
                client._client.request = AsyncMock(
                    side_effect=httpx.ConnectError("Connection refused")
                )

                with pytest.raises(httpx.ConnectError):
                    await client.post(
                        "https://api.example.com/data", json={"key": "value"}
                    )

                mock_send.assert_called_once()
                call_kwargs = mock_send.call_args[1]
                assert call_kwargs["status_code"] == 0
                assert "connection" in call_kwargs["error_message"].lower()

    @pytest.mark.asyncio
    async def test_no_intercept_on_success(self):
        """Verifica que respostas 200 não são interceptadas."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = '{"success": true}'

        with patch(
            "src.utils.http_client.send_api_error", new_callable=AsyncMock
        ) as mock_send:
            async with InterceptedHTTPClient(
                user_id="5521555555555",
                source={"source": "mcp", "tool": "test"},
            ) as client:
                client._client = AsyncMock()
                client._client.request = AsyncMock(return_value=mock_response)

                response = await client.get("https://api.example.com/success")

                assert response.status_code == 200
                mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_disable_interception(self):
        """Verifica que interceptação de erros de rede pode ser desabilitada."""
        with patch(
            "src.utils.http_client.send_api_error", new_callable=AsyncMock
        ) as mock_send:
            async with InterceptedHTTPClient(
                user_id="5521444444444",
                source={"source": "mcp", "tool": "test"},
            ) as client:
                client._client = AsyncMock()
                client._client.request = AsyncMock(
                    side_effect=httpx.TimeoutException("Timeout")
                )

                with pytest.raises(httpx.TimeoutException):
                    await client.get(
                        "https://api.example.com/slow", intercept_errors=False
                    )

                mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_custom_error_status_codes(self):
        """Verifica que custom error status codes funcionam quando passados explicitamente."""
        mock_response = MagicMock()
        mock_response.status_code = 429  # Too Many Requests
        mock_response.text = "Rate limited"

        with patch(
            "src.utils.http_client.send_api_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            async with InterceptedHTTPClient(
                user_id="5521333333333",
                source={"source": "mcp", "tool": "test"},
            ) as client:
                client._client = AsyncMock()
                client._client.request = AsyncMock(return_value=mock_response)

                # Por padrão, nenhum status code é interceptado
                response = await client.get("https://api.example.com/limited")
                mock_send.assert_not_called()

                # Com custom codes, 429 é interceptado
                await client.get(
                    "https://api.example.com/limited", error_status_codes={429}
                )
                mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_all_http_methods_do_not_intercept_by_default(self):
        """Verifica que nenhum método HTTP intercepta status codes por padrão."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Error"

        methods_to_test = ["get", "post", "put", "delete", "patch", "head"]

        with patch(
            "src.utils.http_client.send_api_error", new_callable=AsyncMock
        ) as mock_send:
            async with InterceptedHTTPClient(
                user_id="5521222222222",
                source={"source": "mcp", "tool": "test"},
            ) as client:
                client._client = AsyncMock()
                client._client.request = AsyncMock(return_value=mock_response)

                for method in methods_to_test:
                    method_func = getattr(client, method)
                    response = await method_func("https://api.example.com/test")
                    assert response.status_code == 500

                mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_raises_runtime_error_when_client_not_initialized(self):
        """Verifica que RuntimeError é lançado com mensagem clara quando _client não foi inicializado."""
        client = InterceptedHTTPClient(
            user_id="test",
            source={"source": "mcp", "tool": "test"},
        )
        # _client is None, not initialized via context manager
        with pytest.raises(RuntimeError, match="context manager"):
            await client.request("GET", "https://api.example.com/test")

    def test_raises_runtime_error_sync_when_client_not_initialized(self):
        """Verifica que RuntimeError é lançado com mensagem clara quando _client não foi inicializado (sync)."""
        client = InterceptedHTTPClient(
            user_id="test",
            source={"source": "mcp", "tool": "test"},
            sync=True,
        )
        # _client is None, not initialized via context manager
        with pytest.raises(RuntimeError, match="context manager"):
            client.request_sync("GET", "https://api.example.com/test")

    @pytest.mark.asyncio
    async def test_workflow_source(self):
        """Verifica que source com workflow é passado corretamente ao interceptar erros de rede."""
        with patch(
            "src.utils.http_client.send_api_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            async with InterceptedHTTPClient(
                user_id="5521999999999",
                source={
                    "source": "mcp",
                    "tool": "multi_step_service",
                    "workflow": "iptu_pagamento",
                    "step": "consultar_guias",
                },
            ) as client:
                client._client = AsyncMock()
                client._client.request = AsyncMock(
                    side_effect=httpx.ConnectError("Connection refused")
                )

                with pytest.raises(httpx.ConnectError):
                    await client.get("https://api.example.com/iptu")

                call_kwargs = mock_send.call_args[1]
                assert call_kwargs["source"]["source"] == "mcp"
                assert call_kwargs["source"]["tool"] == "multi_step_service"
                assert call_kwargs["source"]["workflow"] == "iptu_pagamento"
                assert call_kwargs["source"]["step"] == "consultar_guias"


class TestDefaultErrorStatusCodes:
    """Testes para os status codes padrão."""

    def test_default_error_codes(self):
        """Verifica que os status codes padrão estão corretos."""
        expected = {400, 401, 403, 404, 500, 502, 503, 504}
        assert DEFAULT_ERROR_STATUS_CODES == expected

    @pytest.mark.asyncio
    async def test_default_codes_not_intercepted_by_default(self):
        """Verifica que os códigos padrão NÃO são interceptados sem error_status_codes explícito."""
        with patch(
            "src.utils.http_client.send_api_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            for status_code in DEFAULT_ERROR_STATUS_CODES:
                mock_response = MagicMock()
                mock_response.status_code = status_code
                mock_response.text = f"Error {status_code}"

                async with InterceptedHTTPClient(
                    user_id="test_user",
                    source={"source": "mcp", "tool": "test"},
                ) as client:
                    client._client = AsyncMock()
                    client._client.request = AsyncMock(return_value=mock_response)

                    await client.get("https://api.example.com/test")

            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_default_codes_intercepted_when_passed_explicitly(self):
        """Verifica que os códigos padrão SÃO interceptados quando passados explicitamente."""
        with patch(
            "src.utils.http_client.send_api_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            for status_code in DEFAULT_ERROR_STATUS_CODES:
                mock_response = MagicMock()
                mock_response.status_code = status_code
                mock_response.text = f"Error {status_code}"

                async with InterceptedHTTPClient(
                    user_id="test_user",
                    source={"source": "mcp", "tool": "test"},
                ) as client:
                    client._client = AsyncMock()
                    client._client.request = AsyncMock(return_value=mock_response)

                    await client.get(
                        "https://api.example.com/test",
                        error_status_codes=DEFAULT_ERROR_STATUS_CODES,
                    )

            assert mock_send.call_count == len(DEFAULT_ERROR_STATUS_CODES)


class TestInterceptedHTTPClientSync:
    """Testes para o modo sync do InterceptedHTTPClient."""

    def test_does_not_intercept_500_by_default_sync(self):
        """Verifica que erro 500 NÃO é interceptado por padrão em modo sync."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        with patch(
            "src.utils.http_client.send_api_error", new_callable=AsyncMock
        ) as mock_send:
            with InterceptedHTTPClient(
                user_id="5521111111111",
                source={"source": "mcp", "tool": "test"},
                sync=True,
            ) as client:
                client._client = MagicMock()
                client._client.request = MagicMock(return_value=mock_response)

                response = client.get_sync("https://api.example.com/test")

                assert response.status_code == 500
                mock_send.assert_not_called()

    def test_intercepts_500_when_explicitly_configured_sync(self):
        """Verifica que erro 500 É interceptado quando error_status_codes é passado em modo sync."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        with patch(
            "src.utils.http_client.send_api_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            with InterceptedHTTPClient(
                user_id="5521111111111",
                source={"source": "mcp", "tool": "test"},
                sync=True,
            ) as client:
                client._client = MagicMock()
                client._client.request = MagicMock(return_value=mock_response)

                response = client.get_sync(
                    "https://api.example.com/test",
                    error_status_codes={500},
                )

                assert response.status_code == 500

    def test_intercepts_timeout_sync(self):
        """Verifica interceptação de timeout em modo sync."""
        with patch(
            "src.utils.http_client.send_api_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            with InterceptedHTTPClient(
                user_id="5521000000000",
                source={"source": "mcp", "tool": "test"},
                sync=True,
            ) as client:
                client._client = MagicMock()
                client._client.request = MagicMock(
                    side_effect=httpx.TimeoutException("Timeout")
                )

                with pytest.raises(httpx.TimeoutException):
                    client.get_sync("https://api.example.com/slow")

    def test_no_intercept_on_success_sync(self):
        """Verifica que respostas 200 não são interceptadas em modo sync."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = '{"success": true}'

        with patch(
            "src.utils.http_client.send_api_error", new_callable=AsyncMock
        ) as mock_send:
            with InterceptedHTTPClient(
                user_id="5521999999998",
                source={"source": "mcp", "tool": "test"},
                sync=True,
            ) as client:
                client._client = MagicMock()
                client._client.request = MagicMock(return_value=mock_response)

                response = client.get_sync("https://api.example.com/success")

                assert response.status_code == 200
                mock_send.assert_not_called()

    def test_workflow_source_sync(self):
        """Verifica que source com workflow é passado corretamente ao interceptar erros de rede em modo sync."""
        with patch(
            "src.utils.http_client.send_api_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            with InterceptedHTTPClient(
                user_id="5521999999999",
                source={
                    "source": "mcp",
                    "tool": "multi_step_service",
                    "workflow": "poda_de_arvore",
                },
                sync=True,
            ) as client:
                client._client = MagicMock()
                client._client.request = MagicMock(
                    side_effect=httpx.ConnectError("Connection refused")
                )

                with pytest.raises(httpx.ConnectError):
                    client.get_sync("https://api.example.com/poda")

    def test_raises_error_when_using_async_with_sync_mode(self):
        """Verifica que usar 'async with' em modo sync levanta erro."""
        import asyncio

        async def try_async_with():
            async with InterceptedHTTPClient(
                user_id="test",
                source={"source": "mcp", "tool": "test"},
                sync=True,
            ):
                pass

        with pytest.raises(RuntimeError, match="Use 'with' para modo sync"):
            asyncio.run(try_async_with())

    def test_raises_error_when_using_with_async_mode(self):
        """Verifica que usar 'with' em modo async levanta erro."""
        with pytest.raises(RuntimeError, match="Use 'async with' para modo async"):
            with InterceptedHTTPClient(
                user_id="test",
                source={"source": "mcp", "tool": "test"},
                sync=False,  # async mode (default)
            ):
                pass

    def test_all_sync_http_methods(self):
        """Verifica que todos os métodos HTTP sync funcionam."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "OK"

        with InterceptedHTTPClient(
            user_id="test_user",
            source={"source": "mcp", "tool": "test"},
            sync=True,
        ) as client:
            client._client = MagicMock()
            client._client.request = MagicMock(return_value=mock_response)

            # Testa todos os métodos sync
            client.get_sync("https://api.example.com/test")
            client.post_sync("https://api.example.com/test")
            client.put_sync("https://api.example.com/test")
            client.delete_sync("https://api.example.com/test")
            client.patch_sync("https://api.example.com/test")
            client.head_sync("https://api.example.com/test")

            assert client._client.request.call_count == 6
