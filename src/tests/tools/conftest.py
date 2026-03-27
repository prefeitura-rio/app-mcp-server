"""
Configurações e fixtures para os testes de tools.

Este conftest fornece fixtures compartilhadas para testar as tools do MCP Server.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

@pytest.fixture(autouse=True)
def block_real_error_interceptor():
    """Bloqueia chamadas reais ao error interceptor em TODOS os testes."""
    with patch(
        "src.utils.error_interceptor.send_error_to_interceptor", new_callable=AsyncMock
    ) as mock:
        mock.return_value = True
        yield mock


@pytest.fixture
def mock_bigquery():
    """Mock para BigQuery operations."""
    with patch("src.utils.bigquery.get_bigquery_client") as mock_client:
        mock_bq = MagicMock()
        mock_client.return_value = mock_bq
        yield mock_bq


@pytest.fixture
def mock_http_client():
    """Mock para InterceptedHTTPClient."""
    with patch("src.utils.http_client.InterceptedHTTPClient") as mock_class:
        mock_instance = AsyncMock()
        mock_class.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_class.return_value.__aexit__ = AsyncMock(return_value=None)
        yield mock_instance


@pytest.fixture
def sample_user_id():
    """User ID padrão para testes."""
    return "5521999999999"


@pytest.fixture
def sample_address():
    """Endereço padrão para testes."""
    return "Av. Presidente Vargas, 1 - Centro, Rio de Janeiro"
