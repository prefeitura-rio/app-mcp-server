"""
Testes de cenários reais simulados.

Estes testes simulam cenários reais de uso do interceptor com as tools
do sistema, usando mocks para forçar erros específicos.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import aiohttp

from src.utils import error_interceptor


class TestSearchToolScenarios:
    """Cenários de erro da tool search."""

    @pytest.mark.asyncio
    async def test_typesense_connection_error(self):
        """Simula erro de conexão com Typesense."""
        from src.utils.error_interceptor import interceptor

        # Cria uma função de teste que simula o comportamento do Typesense
        @interceptor(source={"source": "mcp", "tool": "typesense"})
        async def mock_hub_search(query: str):
            raise httpx.ConnectError("Connection refused to Typesense")

        with patch(
            "src.utils.error_interceptor.send_general_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            with pytest.raises(httpx.ConnectError):
                await mock_hub_search(query="teste de busca")

            # Verifica que o erro foi interceptado
            mock_send.assert_called()
            call_kwargs = mock_send.call_args[1]
            assert call_kwargs["error_type"] == "ConnectError"

    @pytest.mark.asyncio
    async def test_gemini_timeout(self):
        """Simula timeout na API do Gemini usando uma função decorada de teste."""
        from src.utils.error_interceptor import interceptor

        # Cria uma função de teste que simula o comportamento do Gemini
        @interceptor(source={"source": "mcp", "tool": "gemini"})
        async def mock_gemini_search(query: str):
            raise TimeoutError("Gemini API timeout")

        with patch(
            "src.utils.error_interceptor.send_general_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            with pytest.raises(TimeoutError):
                await mock_gemini_search(query="teste timeout")

            # Verifica que o erro foi interceptado
            mock_send.assert_called()
            call_kwargs = mock_send.call_args[1]
            assert call_kwargs["error_type"] == "TimeoutError"


class TestMemoryToolScenarios:
    """Cenários de erro da tool memory."""

    @pytest.mark.asyncio
    async def test_memory_api_401_unauthorized(self):
        """Simula erro 401 na API de memória usando uma função decorada de teste."""
        from src.utils.error_interceptor import interceptor

        # Cria uma função de teste que simula o comportamento da API de memória
        @interceptor(
            source={"source": "mcp", "tool": "memory"},
            extract_user_id=lambda args, kwargs: kwargs.get("user_id") or args[0],
        )
        async def mock_get_memories(user_id: str):
            raise aiohttp.ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=401,
                message="Unauthorized",
            )

        with patch(
            "src.utils.error_interceptor.send_general_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            with pytest.raises(aiohttp.ClientResponseError):
                await mock_get_memories(user_id="5521999999999")

            # Verifica que o erro foi interceptado
            mock_send.assert_called()
            call_kwargs = mock_send.call_args[1]
            assert call_kwargs["user_id"] == "5521999999999"
            assert call_kwargs["error_type"] == "ClientResponseError"


class TestDividaAtivaScenarios:
    """Cenários de erro da tool divida_ativa."""

    @pytest.mark.asyncio
    async def test_pgm_api_authentication_failure(self):
        """Simula falha de autenticação na API PGM."""
        with patch.object(
            error_interceptor, "send_general_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            with patch(
                "src.tools.divida_ativa.internal_request", new_callable=AsyncMock
            ) as mock_request:
                # Retorna resposta sem access_token
                mock_request.return_value = {"error": "Invalid credentials"}

                from src.tools.divida_ativa import pgm_api

                with pytest.raises(Exception, match="Failed to get PGM access token"):
                    await pgm_api(
                        endpoint="v2/cdas/dividas-contribuinte",
                        consumidor="test",
                        data={"cpfCnpj": "12345678901"},
                    )

    @pytest.mark.asyncio
    async def test_pgm_api_timeout(self):
        """Simula timeout na API PGM."""
        with patch(
            "src.utils.error_interceptor.send_general_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            with patch(
                "src.tools.divida_ativa.internal_request", new_callable=AsyncMock
            ) as mock_request:
                import asyncio

                mock_request.side_effect = asyncio.TimeoutError("Request timeout")

                from src.tools.divida_ativa import pgm_api

                result = await pgm_api(
                    endpoint="v2/cdas/dividas-contribuinte",
                    consumidor="test",
                    data={"cpfCnpj": "12345678901"},
                )

                # Deve retornar erro amigável
                assert result["erro"] is True
                assert "indisponível" in result["motivos"].lower()


class TestEquipmentsToolScenarios:
    """Cenários de erro da tool equipments."""

    @pytest.mark.asyncio
    async def test_bigquery_query_error(self):
        """Simula erro de query no BigQuery."""
        with patch(
            "src.utils.error_interceptor.send_general_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            with patch(
                "src.tools.equipments.pluscode_service.get_bigquery_client"
            ) as mock_client:
                mock_bq = MagicMock()
                mock_bq.query = MagicMock(
                    side_effect=Exception("BigQuery query failed: quota exceeded")
                )
                mock_client.return_value = mock_bq

                with patch(
                    "src.tools.equipments.pluscode_service.get_plus8_coords_from_address"
                ) as mock_coords:
                    mock_coords.return_value = (
                        "23CMP8VV",
                        {"lat": -22.9, "lng": -43.2},
                    )

                    from src.tools.equipments.pluscode_service import (
                        get_pluscode_coords_equipments,
                    )

                    result = await get_pluscode_coords_equipments(
                        address="Av. Presidente Vargas, 1 - Centro"
                    )

                    # Deve retornar erro
                    assert "error" in result


class TestCorAlertScenarios:
    """Cenários de erro da tool cor_alert."""

    @pytest.mark.asyncio
    async def test_geocoding_error_httpx(self):
        """Simula erro de conexão na geocodificação usando uma função decorada de teste."""
        from src.utils.error_interceptor import interceptor

        # Cria uma função async de teste que simula erro de conexão
        @interceptor(source={"source": "mcp", "tool": "cor_alert"})
        async def mock_geocode_with_httpx_error(address: str):
            raise httpx.ConnectError("Connection refused")

        with patch(
            "src.utils.error_interceptor.send_general_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            with pytest.raises(httpx.ConnectError):
                await mock_geocode_with_httpx_error("Rua das Flores, 100 - Centro")

            # Verifica que o erro foi interceptado
            mock_send.assert_called()
            call_kwargs = mock_send.call_args[1]
            assert call_kwargs["error_type"] == "ConnectError"

    @pytest.mark.asyncio
    async def test_geocoding_timeout_httpx(self):
        """Simula timeout na geocodificação usando uma função decorada de teste."""
        from src.utils.error_interceptor import interceptor

        # Cria uma função async de teste que simula timeout
        @interceptor(source={"source": "mcp", "tool": "cor_alert"})
        async def mock_geocode_with_timeout(address: str):
            raise httpx.TimeoutException("Request timeout")

        with patch(
            "src.utils.error_interceptor.send_general_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            with pytest.raises(httpx.TimeoutException):
                await mock_geocode_with_timeout("Rua das Flores, 100")

            # Verifica que o erro foi interceptado
            mock_send.assert_called()
            call_kwargs = mock_send.call_args[1]
            assert call_kwargs["error_type"] == "TimeoutException"


class TestOrchestratorScenarios:
    """Cenários de erro do orchestrator."""

    @pytest.mark.asyncio
    async def test_workflow_not_found(self):
        """Simula workflow não encontrado."""
        from src.tools.multi_step_service.core.orchestrator import Orchestrator
        from src.tools.multi_step_service.core.models import ServiceRequest

        orchestrator = Orchestrator()

        request = ServiceRequest(
            service_name="servico_inexistente",
            user_id="5521999999999",
            payload={},
        )

        result = await orchestrator.execute_workflow(request)

        assert result.error_message is not None
        assert "não encontrado" in result.error_message.lower()

    @pytest.mark.asyncio
    async def test_workflow_execution_error(self):
        """Simula erro durante execução de workflow."""
        with patch(
            "src.utils.error_interceptor.send_general_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            from src.tools.multi_step_service.core.orchestrator import Orchestrator
            from src.tools.multi_step_service.core.models import ServiceRequest

            orchestrator = Orchestrator()

            # Mock de um workflow que falha
            with patch.object(
                orchestrator, "workflows", {"test_workflow": MagicMock()}
            ):
                mock_workflow = MagicMock()
                mock_workflow.return_value.execute = AsyncMock(
                    side_effect=Exception("Workflow crashed")
                )
                orchestrator.workflows["test_workflow"] = mock_workflow

                request = ServiceRequest(
                    service_name="test_workflow",
                    user_id="5521999999999",
                    payload={},
                )

                result = await orchestrator.execute_workflow(request)

                # O orchestrator já trata erros internamente
                assert result.error_message is not None


class TestBigQueryScenarios:
    """Cenários de erro do BigQuery."""

    def test_bigquery_save_error(self):
        """Simula erro ao salvar no BigQuery."""
        with patch.object(
            error_interceptor, "send_general_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            with patch(
                "src.utils.bigquery.get_bigquery_client"
            ) as mock_client:
                mock_bq = MagicMock()
                mock_job = MagicMock()
                mock_job.result = MagicMock(
                    side_effect=Exception("Table not found")
                )
                mock_bq.load_table_from_json.return_value = mock_job
                mock_client.return_value = mock_bq

                from src.utils.bigquery import save_response_in_bq

                with pytest.raises(Exception):
                    save_response_in_bq(
                        data={"test": "data"},
                        endpoint="/test",
                        dataset_id="test_dataset",
                        table_id="test_table",
                    )

    @pytest.mark.asyncio
    async def test_bigquery_query_not_found(self):
        """Simula tabela não encontrada no BigQuery usando função decorada de teste."""
        from src.utils.error_interceptor import interceptor
        from google.api_core.exceptions import NotFound

        # Cria uma função de teste que simula o comportamento do BigQuery
        @interceptor(source={"source": "mcp", "tool": "bigquery"})
        async def mock_get_bigquery_result(query: str):
            raise NotFound("Table not found")

        with patch(
            "src.utils.error_interceptor.send_general_error", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True

            with pytest.raises(NotFound):
                await mock_get_bigquery_result("SELECT * FROM non_existent_table")

            # Verifica que o erro foi interceptado
            mock_send.assert_called()
            call_kwargs = mock_send.call_args[1]
            assert call_kwargs["error_type"] == "NotFound"
