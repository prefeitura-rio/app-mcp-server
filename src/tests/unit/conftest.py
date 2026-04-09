"""
Setup local para testes unitários puros.

Carrega módulos diretamente dos arquivos para evitar importar `src.__init__`
e, com isso, evitar efeitos colaterais da aplicação inteira.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


project_root = Path(__file__).resolve().parents[3]


def _load_module_directly(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


src_pkg = type(sys)("src")
src_pkg.__path__ = [str(project_root / "src")]
sys.modules["src"] = src_pkg

src_config_pkg = type(sys)("src.config")
src_config_pkg.__path__ = [str(project_root / "src" / "config")]
sys.modules["src.config"] = src_config_pkg

src_utils_pkg = type(sys)("src.utils")
src_utils_pkg.__path__ = [str(project_root / "src" / "utils")]
sys.modules["src.utils"] = src_utils_pkg

src_tools_pkg = type(sys)("src.tools")
src_tools_pkg.__path__ = [str(project_root / "src" / "tools")]
sys.modules["src.tools"] = src_tools_pkg

_load_module_directly(
    "src.config.settings", project_root / "src" / "config" / "settings.py"
)
_load_module_directly(
    "src.utils.tool_versioning", project_root / "src" / "utils" / "tool_versioning.py"
)
_load_module_directly(
    "src.utils.datetime_utils", project_root / "src" / "utils" / "datetime_utils.py"
)
_load_module_directly(
    "src.tools.calculator", project_root / "src" / "tools" / "calculator.py"
)
_load_module_directly(
    "src.tools.datetime_tools", project_root / "src" / "tools" / "datetime_tools.py"
)

src_resources_pkg = type(sys)("src.resources")
src_resources_pkg.__path__ = [str(project_root / "src" / "resources")]
sys.modules["src.resources"] = src_resources_pkg

_load_module_directly(
    "src.resources.rio_info", project_root / "src" / "resources" / "rio_info.py"
)


@pytest.fixture(autouse=True)
def block_real_error_interceptor():
    """Bloqueia chamadas reais ao error interceptor em todos os testes unitários."""
    with patch(
        "src.utils.error_interceptor.send_error_to_interceptor", new_callable=AsyncMock
    ) as mock:
        mock.return_value = True
        yield mock


@pytest.fixture
def mock_bigquery():
    """Mock para operações de BigQuery."""
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
