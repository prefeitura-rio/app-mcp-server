"""
Testes para o decorator @interceptor.

Estes testes verificam que o decorator captura e reporta erros corretamente
para funções sync e async, sem modificar as funções originais.
"""

import pytest
from unittest.mock import AsyncMock, patch
import asyncio
import sys

# Usa os módulos carregados pelo conftest
error_interceptor = sys.modules['src.utils.error_interceptor']
interceptor = error_interceptor.interceptor
send_general_error = error_interceptor.send_general_error


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
            extract_source=lambda args, kwargs, base: {**base, "inscricao": kwargs.get("inscricao")},
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


class TestFlownameJsonFormat:
    """Testes para o formato JSON do flowname."""

    def test_flowname_is_valid_json(self):
        """Verifica que flowname é um JSON válido."""
        import json

        source = {"source": "mcp", "tool": "search"}
        flowname = json.dumps(source, indent=2, ensure_ascii=False)

        # Deve ser um JSON válido que pode ser parseado de volta
        parsed = json.loads(flowname)
        assert parsed["source"] == "mcp"
        assert parsed["tool"] == "search"

    def test_flowname_with_workflow(self):
        """Verifica que flowname com workflow é formatado corretamente."""
        import json

        source = {
            "source": "mcp",
            "tool": "multi_step_service",
            "workflow": "iptu_pagamento",
            "step": "consultar_guias",
        }
        flowname = json.dumps(source, indent=2, ensure_ascii=False)

        parsed = json.loads(flowname)
        assert parsed["source"] == "mcp"
        assert parsed["tool"] == "multi_step_service"
        assert parsed["workflow"] == "iptu_pagamento"
        assert parsed["step"] == "consultar_guias"

    def test_flowname_with_unicode(self):
        """Verifica que flowname preserva caracteres unicode."""
        import json

        source = {"source": "mcp", "tool": "search", "query": "São Paulo"}
        flowname = json.dumps(source, indent=2, ensure_ascii=False)

        # Deve conter o caractere ã sem escape
        assert "São Paulo" in flowname
        parsed = json.loads(flowname)
        assert parsed["query"] == "São Paulo"
