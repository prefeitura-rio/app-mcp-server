"""
Configurações e fixtures para os testes de tools.

Este conftest fornece fixtures compartilhadas para testar as tools do MCP Server.
"""

import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Configura variáveis de ambiente antes de imports
os.environ.setdefault("VALID_TOKENS", "test-token")
os.environ.setdefault("ERROR_INTERCEPTOR_URL", "https://test.interceptor.local/api")
os.environ.setdefault("ERROR_INTERCEPTOR_TOKEN", "test-token-123")
os.environ.setdefault("TYPESENSE_ACTIVE", "false")
os.environ.setdefault("TYPESENSE_HUB_SEARCH_URL", "https://test.typesense.local/search")
os.environ.setdefault("TYPESENSE_PARAMETERS", "none")
os.environ.setdefault("GMAPS_API_TOKEN", "test-gmaps-token")
os.environ.setdefault("GOOGLE_MAPS_API_KEY", "test-google-key")
os.environ.setdefault("GOOGLE_MAPS_API_URL", "https://maps.googleapis.com/maps/api/geocode/json")
os.environ.setdefault("NOMINATIM_API_URL", "https://nominatim.openstreetmap.org/search")
os.environ.setdefault("GOOGLE_BIGQUERY_PAGE_SIZE", "100")
os.environ.setdefault("RMI_API_URL", "https://test.rmi.local/api")
os.environ.setdefault("CHATBOT_INTEGRATIONS_URL", "https://test.integrations.local/api")
os.environ.setdefault("CHATBOT_INTEGRATIONS_KEY", "test-key")
os.environ.setdefault("CHATBOT_PGM_API_URL", "https://test.pgm.local/api")
os.environ.setdefault("CHATBOT_PGM_ACCESS_KEY", "test-access-key")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("GCP_SERVICE_ACCOUNT_CREDENTIALS", "e30=")  # base64 de {}
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")
os.environ.setdefault("GEMINI_MODEL", "gemini-2.0-flash")
os.environ.setdefault("SURKAI_API_KEY", "test-surkai-key")
os.environ.setdefault("DATA_DIR", "/tmp")
os.environ.setdefault("EQUIPMENTS_VALID_THEMES", '["geral"]')
os.environ.setdefault("MEMORY_API_URL", "https://test.memory.local/api")
os.environ.setdefault("MEMORY_API_TOKEN", "test-memory-token")

# Configura pytest-asyncio
pytest_plugins = ('pytest_asyncio',)


@pytest.fixture(autouse=True)
def mock_env_variables(monkeypatch):
    """Mock das variáveis de ambiente para todos os testes."""
    monkeypatch.setenv("ERROR_INTERCEPTOR_URL", "https://test.interceptor.local/api")
    monkeypatch.setenv("ERROR_INTERCEPTOR_TOKEN", "test-token-123")
    monkeypatch.setenv("ENVIRONMENT", "test")


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
