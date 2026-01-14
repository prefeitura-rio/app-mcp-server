"""
Configurações e fixtures para os testes do interceptor.

Este conftest usa importação direta para evitar carregar toda a aplicação.
"""

import os
import sys

# IMPORTANTE: Setar variáveis de ambiente ANTES de qualquer import do projeto
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

import pytest
import importlib.util

# Configura pytest-asyncio para modo auto
pytest_plugins = ('pytest_asyncio',)

# Adiciona o diretório raiz ao path
project_root = os.path.join(os.path.dirname(__file__), "..", "..", "..")
if project_root not in sys.path:
    sys.path.insert(0, project_root)


def _load_module_directly(module_name: str, file_path: str):
    """Carrega um módulo diretamente sem passar pelo __init__.py do pacote pai."""
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    return spec, module


# Mock do módulo src.config.env antes de carregar qualquer coisa
class MockEnv:
    ERROR_INTERCEPTOR_URL = os.environ.get("ERROR_INTERCEPTOR_URL", "https://test.local/api")
    ERROR_INTERCEPTOR_TOKEN = os.environ.get("ERROR_INTERCEPTOR_TOKEN", "test-token")


# Registra mocks para evitar imports problemáticos
sys.modules['src'] = type(sys)('src')
sys.modules['src.config'] = type(sys)('src.config')
sys.modules['src.config.env'] = MockEnv


# Carrega os módulos necessários diretamente
_error_interceptor_spec, _error_interceptor = _load_module_directly(
    'src.utils.error_interceptor',
    os.path.join(project_root, 'src', 'utils', 'error_interceptor.py')
)
_error_interceptor_spec.loader.exec_module(_error_interceptor)

_http_client_spec, _http_client = _load_module_directly(
    'src.utils.http_client',
    os.path.join(project_root, 'src', 'utils', 'http_client.py')
)
_http_client_spec.loader.exec_module(_http_client)


@pytest.fixture(autouse=True)
def mock_env_variables(monkeypatch):
    """Mock das variáveis de ambiente necessárias."""
    monkeypatch.setenv("ERROR_INTERCEPTOR_URL", "https://test.interceptor.local/api")
    monkeypatch.setenv("ERROR_INTERCEPTOR_TOKEN", "test-token-123")
    monkeypatch.setenv("ENVIRONMENT", "test")


@pytest.fixture(autouse=True)
def block_real_error_interceptor_calls():
    """
    Bloqueia chamadas reais ao error interceptor em TODOS os testes.

    Isso evita vazamento de erros para o Discord durante os testes.
    Mocka send_error_to_interceptor que é a função de mais baixo nível
    que faz a requisição HTTP real.
    """
    from unittest.mock import AsyncMock, patch

    with patch.object(
        _error_interceptor, 'send_error_to_interceptor', new_callable=AsyncMock
    ) as mock:
        mock.return_value = True
        yield mock


@pytest.fixture
def mock_send_api_error():
    """Mock para send_api_error."""
    from unittest.mock import AsyncMock, patch

    with patch.object(
        _error_interceptor, 'send_api_error', new_callable=AsyncMock
    ) as mock:
        mock.return_value = True
        yield mock


@pytest.fixture
def mock_send_general_error():
    """Mock para send_general_error."""
    from unittest.mock import AsyncMock, patch

    with patch.object(
        _error_interceptor, 'send_general_error', new_callable=AsyncMock
    ) as mock:
        mock.return_value = True
        yield mock
