"""
Setup para testes unitários dos workflows multi-step.

Carrega módulos diretamente dos arquivos para evitar importação do pacote
`src` completo e seus efeitos colaterais.
"""

import importlib.util
import sys
from pathlib import Path


project_root = Path(__file__).resolve().parents[4]


def _load_module(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _ensure_package(module_name: str, path: Path):
    pkg = type(sys)(module_name)
    pkg.__path__ = [str(path)]
    sys.modules[module_name] = pkg
    return pkg


# Carrega o pacote real ANTES dos stubs abaixo. Os testes de `divida_ativa`
# importam o workflow de verdade, que precisa de `core` com conteúdo real
# (`AgentResponse` e afins) — os stubs seguintes são pacotes vazios e só
# servem para pendurar os módulos carregados por caminho.
#
# Isto funcionava por acidente enquanto `src/__init__.py` importava `src.app`
# de forma ansiosa: o grafo real já estava em `sys.modules` quando os stubs
# eram instalados, então eles nunca chegavam a ser usados. Com a importação
# preguiçosa (necessária para o preflight reportar todas as variáveis de
# ambiente faltantes de uma vez), o aquecimento passou a ser explícito.
import src.tools.multi_step_service.core  # noqa: E402,F401
import src.tools.multi_step_service.workflows  # noqa: E402,F401

_ensure_package("src", project_root / "src")
_ensure_package("src.tools", project_root / "src" / "tools")
_ensure_package(
    "src.tools.multi_step_service",
    project_root / "src" / "tools" / "multi_step_service",
)
_ensure_package(
    "src.tools.multi_step_service.core",
    project_root / "src" / "tools" / "multi_step_service" / "core",
)
# `src.tools.multi_step_service.workflows` NÃO é stubado: o `Orchestrator` lê
# a lista `workflows` desse pacote em tempo de execução. O aquecimento acima já
# o deixou em `sys.modules` com o `__path__` correto, então os stubs de
# subpacotes logo abaixo continuam funcionando.
_ensure_package(
    "src.tools.multi_step_service.workflows.poda_de_arvore",
    project_root
    / "src"
    / "tools"
    / "multi_step_service"
    / "workflows"
    / "poda_de_arvore",
)
_ensure_package(
    "src.tools.multi_step_service.workflows.poda_de_arvore.integrations",
    project_root
    / "src"
    / "tools"
    / "multi_step_service"
    / "workflows"
    / "poda_de_arvore"
    / "integrations",
)
_ensure_package(
    "src.tools.multi_step_service.workflows.poda_de_arvore.api",
    project_root
    / "src"
    / "tools"
    / "multi_step_service"
    / "workflows"
    / "poda_de_arvore"
    / "api",
)
_ensure_package(
    "src.tools.multi_step_service.workflows.iptu_pagamento",
    project_root
    / "src"
    / "tools"
    / "multi_step_service"
    / "workflows"
    / "iptu_pagamento",
)
_ensure_package(
    "src.tools.multi_step_service.workflows.iptu_pagamento.helpers",
    project_root
    / "src"
    / "tools"
    / "multi_step_service"
    / "workflows"
    / "iptu_pagamento"
    / "helpers",
)
_ensure_package(
    "src.tools.multi_step_service.workflows.iptu_pagamento.core",
    project_root
    / "src"
    / "tools"
    / "multi_step_service"
    / "workflows"
    / "iptu_pagamento"
    / "core",
)

_load_module(
    "src.tools.multi_step_service.core.models",
    project_root / "src" / "tools" / "multi_step_service" / "core" / "models.py",
)
_load_module(
    "src.tools.multi_step_service.workflows.poda_de_arvore.models",
    project_root
    / "src"
    / "tools"
    / "multi_step_service"
    / "workflows"
    / "poda_de_arvore"
    / "models.py",
)
_load_module(
    "src.tools.multi_step_service.workflows.poda_de_arvore.state_helpers",
    project_root
    / "src"
    / "tools"
    / "multi_step_service"
    / "workflows"
    / "poda_de_arvore"
    / "state_helpers.py",
)
_load_module(
    "src.tools.multi_step_service.workflows.poda_de_arvore.templates",
    project_root
    / "src"
    / "tools"
    / "multi_step_service"
    / "workflows"
    / "poda_de_arvore"
    / "templates.py",
)
_load_module(
    "src.tools.multi_step_service.workflows.poda_de_arvore.integrations.ticket_builder",
    project_root
    / "src"
    / "tools"
    / "multi_step_service"
    / "workflows"
    / "poda_de_arvore"
    / "integrations"
    / "ticket_builder.py",
)
_load_module(
    "src.tools.multi_step_service.workflows.iptu_pagamento.core.models",
    project_root
    / "src"
    / "tools"
    / "multi_step_service"
    / "workflows"
    / "iptu_pagamento"
    / "core"
    / "models.py",
)
_load_module(
    "src.tools.multi_step_service.workflows.iptu_pagamento.helpers.utils",
    project_root
    / "src"
    / "tools"
    / "multi_step_service"
    / "workflows"
    / "iptu_pagamento"
    / "helpers"
    / "utils.py",
)
_load_module(
    "src.tools.multi_step_service.workflows.iptu_pagamento.helpers.state_helpers",
    project_root
    / "src"
    / "tools"
    / "multi_step_service"
    / "workflows"
    / "iptu_pagamento"
    / "helpers"
    / "state_helpers.py",
)
