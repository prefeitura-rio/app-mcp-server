"""Smoke test do factory da aplicação (`src/app.py`).

`create_app()` é o caminho que monta o servidor MCP de produção e registra as
tools, mas estava com 0% de cobertura: nenhum teste importava `src.app`
(`test_main.py` o substitui por um stub de propósito, para testar o
entrypoint isoladamente). Na prática, uma tool sumir do registro ou um import
quebrar em `src/app.py` passaria pelo CI inteiro sem ninguém notar.

O objetivo aqui não é cobertura de linha e sim travar o contrato de
inicialização: o factory constrói, registra o conjunto esperado de tools,
respeita `EXCLUDED_TOOLS` e expõe as rotas HTTP da Dívida Ativa.
"""

import importlib
import sys

import pytest

from src.health import state as health_state


# Conjunto travado de propósito: uma tool a mais ou a menos precisa ser uma
# decisão consciente de quem mexe, não um efeito colateral silencioso.
EXPECTED_TOOLS = {
    "calculator_add",
    "calculator_divide",
    "calculator_multiply",
    "calculator_power",
    "calculator_subtract",
    "equipments_by_address",
    "equipments_instructions",
    "get_user_memory",
    "google_search",
    "multi_step_service",
    "report_incident",
    "time_current",
    "upsert_user_memory",
    "web_search_surkai",
}


@pytest.fixture
def app_module():
    """Importa `src.app` num `sys.modules` limpo e devolve o original no fim.

    Duas coisas precisam ser isoladas:

    1. `src/tests/unit/workflows/conftest.py` instala stubs permanentes de
       `src.tools.multi_step_service.*` em `sys.modules` no momento da coleta.
       Se este teste rodar depois disso, `src.app` importa os stubs vazios e
       quebra. Purgar `src.*` antes do import elimina a dependência de ordem
       em vez de torcer pela ordem alfabética.
    2. `create_app()` chama `set_ready(True)` no fim. Sem restaurar, este
       módulo influenciaria os testes de `/health/ready`, que dependem do
       estado inicial `False`.

    O snapshot é devolvido no teardown, então os módulos que os outros testes
    já têm em mãos continuam sendo os mesmos objetos.
    """
    ready_antes = health_state.is_ready()
    snapshot = {
        name: mod for name, mod in sys.modules.items() if name.startswith("src")
    }

    for name in list(sys.modules):
        if name.startswith("src"):
            del sys.modules[name]

    try:
        yield importlib.import_module("src.app")
    finally:
        for name in list(sys.modules):
            if name.startswith("src"):
                del sys.modules[name]
        sys.modules.update(snapshot)
        health_state.set_ready(ready_antes)


def _registered_tool_names(mcp) -> set:
    return set(mcp._tool_manager._tools.keys())


def _registered_route_paths(mcp) -> set:
    return {
        route.path for route in mcp._custom_starlette_routes if hasattr(route, "path")
    }


def test_create_app_registers_expected_tools(app_module):
    mcp = app_module.create_app()
    assert _registered_tool_names(mcp) == EXPECTED_TOOLS


def test_module_level_instance_matches_factory(app_module):
    """`mcp = create_app()` no topo do módulo é o que `src/main.py` serve."""
    assert _registered_tool_names(app_module.mcp) == EXPECTED_TOOLS


def test_excluded_tools_are_not_registered(app_module):
    """`EXCLUDED_TOOLS` precisa realmente barrar o registro."""
    registradas = _registered_tool_names(app_module.create_app())
    for excluida in app_module.EXCLUDED_TOOLS:
        assert excluida not in registradas


def test_divida_ativa_http_routes_are_exposed(app_module):
    """v1 (deprecated) e v2 precisam coexistir enquanto os clientes migram."""
    mcp = app_module.create_app()
    paths = _registered_route_paths(mcp)

    assert "/consulta_debitos" in paths
    assert "/emitir_guia" in paths
    assert "/v2/emitir_guia" in paths
    assert "/v2/emitir_guia_regularizacao" in paths


def test_health_routes_are_exposed(app_module):
    """`/health/ready` é o que o Kubernetes consulta; sumir dele derruba o deploy."""
    mcp = app_module.create_app()
    paths = _registered_route_paths(mcp)

    assert "/health" in paths
    assert "/health/ready" in paths
