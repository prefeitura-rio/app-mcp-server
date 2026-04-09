"""
Testes para o decorator @interceptor.

Estes testes verificam que o decorator captura e reporta erros corretamente
para funções sync e async, sem modificar as funções originais.
"""

import pytest
from unittest.mock import AsyncMock, patch
import asyncio
import json
import sys

# Usa os módulos carregados pelo conftest
error_interceptor = sys.modules["src.utils.error_interceptor"]
interceptor = error_interceptor.interceptor
send_general_error = error_interceptor.send_general_error
real_send_error_to_interceptor = error_interceptor.send_error_to_interceptor


class TestInterceptorDecoratorAsync:
    """Testes para funções async decoradas com @interceptor."""

    @pytest.mark.asyncio
    async def test_interceptor_captures_exception_async(self):
        """Verifica que exceções são capturadas e reportadas."""

        @interceptor(source={"source": "mcp", "tool": "test"})
        async def failing_function():
            raise ValueError("Erro de teste")

        with patch.object(
            error_interceptor, "send_general_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            with pytest.raises(ValueError, match="Erro de teste"):
                await failing_function()

            # Verifica que o erro foi reportado
            mock_send.assert_called_once()
            call_kwargs = mock_send.call_args[1]
            assert call_kwargs["source"]["source"] == "mcp"
            assert call_kwargs["source"]["tool"] == "test"
            assert call_kwargs["error_type"] == "ValueError"
            assert "Erro de teste" in call_kwargs["error_message"]

    @pytest.mark.asyncio
    async def test_interceptor_extracts_user_id_from_kwargs(self):
        """Verifica extração de user_id dos kwargs e input_body."""

        @interceptor(
            source={"source": "mcp", "tool": "test_tool"},
            extract_user_id=lambda args, kwargs: kwargs.get("user_id", "unknown"),
        )
        async def failing_function(user_id: str):
            raise RuntimeError("Falha simulada")

        with patch.object(
            error_interceptor, "send_general_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            with pytest.raises(RuntimeError):
                await failing_function(user_id="5521999999999")

            call_kwargs = mock_send.call_args[1]
            assert call_kwargs["user_id"] == "5521999999999"
            # Verifica que input_body contém os parâmetros da função
            assert call_kwargs["input_body"]["user_id"] == "5521999999999"

    @pytest.mark.asyncio
    async def test_interceptor_extracts_user_id_from_args(self):
        """Verifica extração de user_id dos args posicionais."""

        @interceptor(
            source={"source": "mcp", "tool": "test_tool"},
            extract_user_id=lambda args, kwargs: args[0] if args else "unknown",
        )
        async def failing_function(user_id: str, data: dict):
            raise ConnectionError("Conexão perdida")

        with patch.object(
            error_interceptor, "send_general_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            with pytest.raises(ConnectionError):
                await failing_function("5521888888888", {"key": "value"})

            call_kwargs = mock_send.call_args[1]
            assert call_kwargs["user_id"] == "5521888888888"

    @pytest.mark.asyncio
    async def test_interceptor_success_no_report(self):
        """Verifica que funções bem-sucedidas não reportam erro."""

        @interceptor(source={"source": "mcp", "tool": "test"})
        async def successful_function():
            return {"success": True}

        with patch.object(
            error_interceptor, "send_general_error", new_callable=AsyncMock
        ) as mock_send:
            result = await successful_function()

            assert result == {"success": True}
            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_interceptor_with_custom_error_types(self):
        """Verifica que apenas tipos de erro específicos são interceptados."""

        @interceptor(
            source={"source": "mcp", "tool": "test_tool"},
            error_types=(ValueError, TypeError),
        )
        async def failing_function(error_type: str):
            if error_type == "value":
                raise ValueError("Value error")
            elif error_type == "type":
                raise TypeError("Type error")
            else:
                raise RuntimeError("Runtime error")

        with patch.object(
            error_interceptor, "send_general_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            # ValueError deve ser interceptado
            with pytest.raises(ValueError):
                await failing_function("value")
            assert mock_send.call_count == 1

            # TypeError deve ser interceptado
            with pytest.raises(TypeError):
                await failing_function("type")
            assert mock_send.call_count == 2

            # RuntimeError NÃO deve ser interceptado (não está na lista)
            with pytest.raises(RuntimeError):
                await failing_function("runtime")
            # O count não aumentou porque RuntimeError não é interceptado
            assert mock_send.call_count == 2

    @pytest.mark.asyncio
    async def test_interceptor_includes_dynamic_source(self):
        """Verifica que source dinâmico é incluído no report via extract_source."""

        @interceptor(
            source={"source": "mcp", "tool": "test_tool"},
            extract_source=lambda args, kwargs, base: {
                **base,
                "inscricao": kwargs.get("inscricao"),
            },
        )
        async def failing_function(inscricao: str):
            raise Exception("Falha na consulta")

        with patch.object(
            error_interceptor, "send_general_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            with pytest.raises(Exception):
                await failing_function(inscricao="12345678")

            call_kwargs = mock_send.call_args[1]
            assert call_kwargs["source"]["inscricao"] == "12345678"
            assert "function" in call_kwargs["source"]

    @pytest.mark.asyncio
    async def test_interceptor_with_workflow_source(self):
        """Verifica que source com workflow é serializado corretamente."""

        @interceptor(
            source={
                "source": "mcp",
                "tool": "multi_step_service",
                "workflow": "iptu_pagamento",
            }
        )
        async def workflow_function():
            raise Exception("Erro no workflow")

        with patch.object(
            error_interceptor, "send_general_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            with pytest.raises(Exception):
                await workflow_function()

            call_kwargs = mock_send.call_args[1]
            assert call_kwargs["source"]["source"] == "mcp"
            assert call_kwargs["source"]["tool"] == "multi_step_service"
            assert call_kwargs["source"]["workflow"] == "iptu_pagamento"


class TestInterceptorDecoratorSync:
    """Testes para funções sync decoradas com @interceptor."""

    def test_interceptor_captures_exception_sync(self):
        """Verifica que exceções em funções sync são capturadas."""

        @interceptor(source={"source": "mcp", "tool": "test_sync"})
        def failing_sync_function():
            raise ValueError("Erro sync de teste")

        # Mock para evitar envio real ao interceptor
        with patch.object(
            error_interceptor, "send_general_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            # Para funções sync, o interceptor tenta usar asyncio.run ou create_task
            # Vamos apenas verificar que a exceção é re-levantada
            with pytest.raises(ValueError, match="Erro sync de teste"):
                failing_sync_function()

    def test_interceptor_success_sync(self):
        """Verifica que funções sync bem-sucedidas retornam normalmente."""

        @interceptor(source={"source": "mcp", "tool": "sync_tool"})
        def successful_sync_function(x: int, y: int):
            return x + y

        result = successful_sync_function(2, 3)
        assert result == 5


class TestInterceptorWithTimeout:
    """Testes para cenários de timeout."""

    @pytest.mark.asyncio
    async def test_interceptor_captures_timeout_error(self):
        """Verifica que TimeoutError é capturado."""

        @interceptor(source={"source": "mcp", "tool": "timeout_tool"})
        async def timeout_function():
            raise asyncio.TimeoutError("Request timeout")

        with patch.object(
            error_interceptor, "send_general_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            with pytest.raises(asyncio.TimeoutError):
                await timeout_function()

            mock_send.assert_called_once()
            call_kwargs = mock_send.call_args[1]
            assert call_kwargs["error_type"] == "TimeoutError"


class TestInterceptorWithHttpErrors:
    """Testes para cenários de erros HTTP simulados."""

    @pytest.mark.asyncio
    async def test_interceptor_captures_connection_error(self):
        """Verifica que erros de conexão são capturados."""

        @interceptor(source={"source": "mcp", "tool": "http_tool"})
        async def connection_error_function():
            raise ConnectionError("Falha na conexão com o servidor")

        with patch.object(
            error_interceptor, "send_general_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            with pytest.raises(ConnectionError):
                await connection_error_function()

            mock_send.assert_called_once()
            call_kwargs = mock_send.call_args[1]
            assert call_kwargs["error_type"] == "ConnectionError"
            assert "Falha na conexão" in call_kwargs["error_message"]

    @pytest.mark.asyncio
    async def test_interceptor_includes_traceback(self):
        """Verifica que traceback é incluído no report."""

        @interceptor(source={"source": "mcp", "tool": "tb_tool"})
        async def nested_error_function():
            def inner():
                raise KeyError("chave não encontrada")

            inner()

        with patch.object(
            error_interceptor, "send_general_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            with pytest.raises(KeyError):
                await nested_error_function()

            call_kwargs = mock_send.call_args[1]
            assert call_kwargs["traceback"] is not None
            assert "inner" in call_kwargs["traceback"]
            assert "KeyError" in call_kwargs["traceback"]


class TestSerializeSource:
    """Testes para a função serialize_source usada para gerar flownames."""

    def test_serialize_source_simple(self):
        """Verifica serialização de source simples."""
        from src.utils.error_interceptor import serialize_source

        source = {"source": "mcp", "tool": "search"}
        flowname = serialize_source(source)

        assert "source=mcp" in flowname
        assert "tool=search" in flowname
        assert "|" in flowname

    def test_serialize_source_with_workflow(self):
        """Verifica serialização de source com workflow."""
        from src.utils.error_interceptor import serialize_source

        source = {
            "source": "mcp",
            "tool": "multi_step_service",
            "workflow": "iptu_pagamento",
            "step": "consultar_guias",
        }
        flowname = serialize_source(source)

        assert "source=mcp" in flowname
        assert "tool=multi_step_service" in flowname
        assert "workflow=iptu_pagamento" in flowname
        assert "step=consultar_guias" in flowname

    def test_serialize_source_with_unicode(self):
        """Verifica que serialize_source preserva caracteres unicode."""
        from src.utils.error_interceptor import serialize_source

        source = {"source": "mcp", "tool": "search", "query": "São Paulo"}
        flowname = serialize_source(source)

        assert "São Paulo" in flowname

    def test_send_api_error_uses_serialize_source(self):
        """Verifica que send_api_error chama serialize_source para gerar o flowname."""
        import asyncio
        from unittest.mock import AsyncMock, patch

        source = {"source": "mcp", "tool": "search"}

        with (
            patch.object(
                error_interceptor,
                "serialize_source",
                wraps=error_interceptor.serialize_source,
            ) as mock_serialize,
            patch.object(
                error_interceptor, "send_error_to_interceptor", new_callable=AsyncMock
            ) as mock_send,
        ):
            mock_send.return_value = True

            asyncio.run(
                error_interceptor.send_api_error(
                    user_id="test_user",
                    source=source,
                    api_endpoint="https://api.example.com/test",
                    request_body={},
                    status_code=500,
                    error_message="Error",
                )
            )

            mock_serialize.assert_called_once()
            assert mock_serialize.call_args.args[0]["source"] == "mcp"
            assert mock_serialize.call_args.args[0]["tool"] == "search"
            assert mock_serialize.call_args.args[0]["environment"] == "test"

    def test_send_general_error_uses_serialize_source(self):
        """Verifica que send_general_error chama serialize_source para gerar o flowname."""
        import asyncio
        from unittest.mock import AsyncMock, patch

        source = {"source": "mcp", "tool": "search"}

        with (
            patch.object(
                error_interceptor,
                "serialize_source",
                wraps=error_interceptor.serialize_source,
            ) as mock_serialize,
            patch.object(
                error_interceptor, "send_error_to_interceptor", new_callable=AsyncMock
            ) as mock_send,
        ):
            mock_send.return_value = True

            asyncio.run(
                error_interceptor.send_general_error(
                    user_id="test_user",
                    source=source,
                    error_type="ValueError",
                    error_message="Test error",
                )
            )

            mock_serialize.assert_called_once()
            assert mock_serialize.call_args.args[0]["source"] == "mcp"
            assert mock_serialize.call_args.args[0]["tool"] == "search"
            assert mock_serialize.call_args.args[0]["environment"] == "test"


class TestInterceptorEnvironmentControls:
    """Testes do comportamento por ambiente do interceptor."""

    @pytest.mark.asyncio
    async def test_send_error_to_interceptor_skips_during_tests(self):
        with (
            patch.object(error_interceptor.env, "ENVIRONMENT", "test"),
            patch.object(error_interceptor.env, "IS_LOCAL", False),
            patch.object(error_interceptor.httpx, "AsyncClient") as mock_client,
        ):
            result = await real_send_error_to_interceptor(
                customer_whatsapp_number="unknown",
                flowname="source=mcp | tool=test",
                api_endpoint="internal://Exception",
                input_body={"hello": "world"},
                http_status_code=0,
                error_message="erro",
                source={"source": "mcp", "tool": "test"},
            )

        assert result is False
        mock_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_general_error_includes_environment_in_source(self):
        with (
            patch.object(error_interceptor.env, "ENVIRONMENT", "staging"),
            patch.object(error_interceptor.env, "IS_LOCAL", False),
            patch.object(
                error_interceptor, "send_error_to_interceptor", new_callable=AsyncMock
            ) as mock_send,
        ):
            mock_send.return_value = True

            await error_interceptor.send_general_error(
                user_id="unknown",
                source={"source": "mcp", "tool": "divida_ativa"},
                error_type="Exception",
                error_message="erro",
            )

        sent_source = mock_send.call_args.kwargs["source"]
        assert sent_source["environment"] == "staging"

    @pytest.mark.asyncio
    async def test_send_error_to_interceptor_includes_environment_in_payload(self):
        class DummyResponse:
            status_code = 200
            text = "ok"

        class DummyClient:
            def __init__(self):
                self.post_calls = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def post(self, *args, **kwargs):
                self.post_calls.append((args, kwargs))
                return DummyResponse()

        dummy_client = DummyClient()

        with (
            patch.object(error_interceptor.env, "ENVIRONMENT", "staging"),
            patch.object(error_interceptor.env, "IS_LOCAL", False),
            patch.object(
                error_interceptor.env, "ERROR_INTERCEPTOR_URL", "https://test.local/api"
            ),
            patch.object(error_interceptor.env, "ERROR_INTERCEPTOR_TOKEN", "token"),
            patch.object(error_interceptor, "_should_report_errors", return_value=True),
            patch.object(
                error_interceptor.httpx, "AsyncClient", return_value=dummy_client
            ),
        ):
            result = await real_send_error_to_interceptor(
                customer_whatsapp_number="unknown",
                flowname="source=mcp | tool=test",
                api_endpoint="internal://Exception",
                input_body={"hello": "world"},
                http_status_code=0,
                error_message="erro",
                source={"source": "mcp", "tool": "test"},
            )

        assert result is True
        payload = dummy_client.post_calls[0][1]["json"]
        parsed_source = json.loads(payload["source"])
        assert parsed_source["environment"] == "staging"
