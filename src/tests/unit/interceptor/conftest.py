"""
Configurações e fixtures para os testes do interceptor.

Este conftest usa importação direta para evitar carregar toda a aplicação.
"""

import os
import sys

import pytest
import importlib.util

# Adiciona o diretório raiz ao path
project_root = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
if project_root not in sys.path:
    sys.path.insert(0, project_root)


def _load_module_directly(module_name: str, file_path: str):
    """Carrega um módulo diretamente sem passar pelo __init__.py do pacote pai."""
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    return spec, module


# Mock do módulo src.config.env antes de carregar qualquer coisa.
# NOTA: este mock substitui sys.modules["src.config.env"] globalmente (linha
# abaixo) e nunca é restaurado, então vaza pra outros módulos de teste que
# importarem src.config.env depois deste conftest. Por isso MockEnv precisa
# espelhar os atributos públicos de env.py que código sob teste lê em runtime
# (ex: src.tools.tts lê env.TTS_*). Ver "Noticed improvements" no PR do TTS.
class MockEnv:
    ENVIRONMENT = os.environ.get("ENVIRONMENT", "test")
    IS_LOCAL = os.environ.get("IS_LOCAL", "false") == "true"
    ERROR_INTERCEPTOR_URL = os.environ.get(
        "ERROR_INTERCEPTOR_URL", "https://test.local/api"
    )
    ERROR_INTERCEPTOR_TOKEN = os.environ.get("ERROR_INTERCEPTOR_TOKEN", "test-token")
    # TTS (espelha env.py — ver ADR-038). Necessário porque src.tools.tts
    # importa src.config.env e, via vazamento deste mock, resolve pra MockEnv.
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
    TTS_PROVIDER = os.environ.get("TTS_PROVIDER", "google")
    TTS_GEMINI_MODEL = os.environ.get(
        "TTS_GEMINI_MODEL", "gemini-2.5-flash-preview-tts"
    )
    TTS_GEMINI_VOICE = os.environ.get("TTS_GEMINI_VOICE", "Sulafat")
    TTS_GEMINI_STYLE_PROMPT = os.environ.get(
        "TTS_GEMINI_STYLE_PROMPT",
        "Fale em português do Brasil com sotaque carioca, tom acolhedor e natural.",
    )


# Registra mocks para evitar imports problemáticos
sys.modules["src"] = type(sys)("src")
sys.modules["src.config"] = type(sys)("src.config")
sys.modules["src.config.env"] = MockEnv


# Carrega os módulos necessários diretamente
_error_interceptor_spec, _error_interceptor = _load_module_directly(
    "src.utils.error_interceptor",
    os.path.join(project_root, "src", "utils", "error_interceptor.py"),
)
_error_interceptor_spec.loader.exec_module(_error_interceptor)

_http_client_spec, _http_client = _load_module_directly(
    "src.utils.http_client",
    os.path.join(project_root, "src", "utils", "http_client.py"),
)
_http_client_spec.loader.exec_module(_http_client)


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
        _error_interceptor, "send_error_to_interceptor", new_callable=AsyncMock
    ) as mock:
        mock.return_value = True
        yield mock


@pytest.fixture
def mock_send_api_error():
    """Mock para send_api_error."""
    from unittest.mock import AsyncMock, patch

    with patch.object(
        _error_interceptor, "send_api_error", new_callable=AsyncMock
    ) as mock:
        mock.return_value = True
        yield mock


@pytest.fixture
def mock_send_general_error():
    """Mock para send_general_error."""
    from unittest.mock import AsyncMock, patch

    with patch.object(
        _error_interceptor, "send_general_error", new_callable=AsyncMock
    ) as mock:
        mock.return_value = True
        yield mock
