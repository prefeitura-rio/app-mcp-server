"""
Setup local para testes unitários puros.

Carrega módulos diretamente dos arquivos para evitar importar `src.__init__`
e, com isso, evitar efeitos colaterais da aplicação inteira.
"""

import importlib.util
import sys
from pathlib import Path


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

_load_module_directly("src.config.settings", project_root / "src" / "config" / "settings.py")
_load_module_directly("src.utils.tool_versioning", project_root / "src" / "utils" / "tool_versioning.py")
_load_module_directly("src.utils.datetime_utils", project_root / "src" / "utils" / "datetime_utils.py")
_load_module_directly("src.tools.calculator", project_root / "src" / "tools" / "calculator.py")
