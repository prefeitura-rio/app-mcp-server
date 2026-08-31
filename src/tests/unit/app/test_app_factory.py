"""Smoke test do factory da aplicação (`src/app.py`).

`create_app()` é o caminho que monta o servidor MCP de produção e registra as
tools, mas estava com 0% de cobertura: nenhum teste importava `src.app`
(`test_main.py` o substitui por um stub de propósito, para testar o entrypoint
isoladamente). Na prática, uma tool sumir do registro ou um import quebrar em
`src/app.py` passaria pelo CI inteiro sem ninguém notar.

O objetivo não é cobertura de linha e sim travar o contrato de inicialização.

Duas coisas aqui precisam ser independentes de ambiente, porque `src/app.py`
se comporta de forma diferente conforme as variáveis de ambiente:

- `EXCLUDED_TOOLS` vem do ambiente. Numa máquina de desenvolvimento costuma
  excluir três tools; no CI, só `user_feedback`. Por isso o teste afirma
  `registradas == catálogo - excluídas`, não uma lista fixa.
- `IS_LOCAL` decide entre **duas implementações distintas** de `FastMCP`:
  `mcp.server.fastmcp` (SDK) quando local, `fastmcp` (pacote) em produção. As
  duas guardam as rotas em atributos privados de nomes diferentes, então o
  teste constrói o app ASGI pela API pública e lê as rotas de lá — o que
  também é mais forte: valida que a rota é de fato servida, não que consta de
  uma lista interna.
"""

import asyncio
import importlib
import sys
from unittest.mock import MagicMock

import pytest

from src.config.env import EXCLUDED_TOOLS
from src.health import state as health_state


# Catálogo completo, travado de propósito: uma tool a mais ou a menos precisa
# ser decisão consciente de quem mexe, não efeito colateral silencioso. Inclui
# as excluíveis — o que cada ambiente registra é este conjunto menos
# `EXCLUDED_TOOLS`.
TOOL_CATALOG = {
    "calculator_add",
    "calculator_divide",
    "calculator_multiply",
    "calculator_power",
    "calculator_subtract",
    "dharma_search_tool",
    "equipments_by_address",
    "equipments_instructions",
    "get_user_memory",
    "google_search",
    "greeting_format",
    "multi_step_service",
    "report_incident",
    "rock_in_rio_lineup",
    "time_current",
    "upsert_user_memory",
    "user_feedback",
    "web_search_surkai",
}


@pytest.fixture
def app_module():
    """Importa `src.app` num `sys.modules` limpo e devolve o original no fim.

    Duas coisas precisam ser isoladas:

    1. `src/tests/unit/workflows/conftest.py` instala stubs permanentes de
       `src.tools.multi_step_service.*` em `sys.modules` no momento da coleta.
       Se este teste rodar depois disso, `src.app` importa os stubs vazios e
       quebra. Purgar `src.*` antes do import elimina a dependência de ordem em
       vez de torcer pela ordem alfabética.
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


def _served_route_paths(mcp) -> set:
    """Rotas que o app ASGI realmente serve.

    `http_app` é o nome no pacote `fastmcp`; `streamable_http_app`, no SDK
    `mcp`. Ver o docstring do módulo sobre por que as duas implementações
    coexistem.
    """
    builder = getattr(mcp, "http_app", None) or getattr(
        mcp, "streamable_http_app", None
    )
    assert builder is not None, (
        f"{type(mcp).__module__}.{type(mcp).__name__} não expõe nem `http_app` "
        "nem `streamable_http_app`"
    )
    return {route.path for route in builder().routes if hasattr(route, "path")}


def _expected_tools() -> set:
    return TOOL_CATALOG - set(EXCLUDED_TOOLS)


def test_excluded_tools_belong_to_the_catalog():
    """`EXCLUDED_TOOLS` com um nome que não existe é engano silencioso.

    Excluir `google_serach` por typo não dá erro nenhum: a tool continua
    registrada e quem configurou acha que desligou.
    """
    assert set(EXCLUDED_TOOLS) <= TOOL_CATALOG, (
        f"nomes fora do catálogo: {sorted(set(EXCLUDED_TOOLS) - TOOL_CATALOG)}"
    )


def test_create_app_registers_expected_tools(app_module):
    assert _registered_tool_names(app_module.create_app()) == _expected_tools()


def test_module_level_instance_matches_factory(app_module):
    """`mcp = create_app()` no topo do módulo é o que `src/main.py` serve."""
    assert _registered_tool_names(app_module.mcp) == _expected_tools()


def test_excluded_tools_are_not_registered(app_module):
    """`EXCLUDED_TOOLS` precisa realmente barrar o registro."""
    registradas = _registered_tool_names(app_module.create_app())
    for excluida in EXCLUDED_TOOLS:
        assert excluida not in registradas


def test_divida_ativa_http_routes_are_served(app_module):
    """v1 (deprecated) e v2 precisam coexistir enquanto os clientes migram."""
    paths = _served_route_paths(app_module.create_app())

    assert "/consulta_debitos" in paths
    assert "/emitir_guia" in paths
    assert "/emitir_guia_regularizacao" in paths
    assert "/v2/emitir_guia" in paths
    assert "/v2/emitir_guia_regularizacao" in paths


def test_health_routes_are_served(app_module):
    """`/health/ready` é o que o Kubernetes consulta; sumir dele derruba o deploy."""
    paths = _served_route_paths(app_module.create_app())

    assert "/health" in paths
    assert "/health/ready" in paths
    assert "/health/detail" in paths


def test_mcp_endpoint_is_served(app_module):
    """`/mcp` é o endpoint do protocolo — é o serviço inteiro."""
    assert "/mcp" in _served_route_paths(app_module.create_app())


@pytest.fixture
def importar_app_com_ambiente(monkeypatch):
    """Reimporta `src.app` depois de o teste ajustar o ambiente.

    `IS_LOCAL` e `EXCLUDED_TOOLS` são lidas no import de `src.config.env`: com o
    módulo já carregado, trocá-las não tem efeito. Mesma higiene do
    `app_module` — purga `src.*` antes do import e devolve o snapshot no fim.
    """
    ready_antes = health_state.is_ready()
    snapshot = {
        name: mod for name, mod in sys.modules.items() if name.startswith("src")
    }

    def _importar(**variaveis):
        for nome, valor in variaveis.items():
            monkeypatch.setenv(nome, valor)
        for name in list(sys.modules):
            if name.startswith("src"):
                del sys.modules[name]
        return importlib.import_module("src.app")

    try:
        yield _importar
    finally:
        for name in list(sys.modules):
            if name.startswith("src"):
                del sys.modules[name]
        sys.modules.update(snapshot)
        health_state.set_ready(ready_antes)


async def _laco_parado():
    """Substitui um laço de background: fica vivo até ser cancelado."""
    await asyncio.Event().wait()


def _lifespan_isolado(app_module, monkeypatch):
    """Devolve `(lifespan, chamadas)` com o trabalho de fundo neutralizado.

    O lifespan é uma closure de `create_app()`. Para alcançá-lo sem depender de
    atributo privado do FastMCP — cujos nomes diferem entre as duas
    implementações, ver o docstring do módulo — a própria classe é trocada por
    um duplo que guarda os kwargs de construção.

    Tudo o que sai para rede ou disco no startup é substituído: o que este teste
    observa é a decisão de ligar ou não o line-up, e nada mais.
    """
    construcao = {}

    class FastMCPDuplo(MagicMock):
        def __init__(self, **kwargs):
            super().__init__()
            construcao.update(kwargs)

    monkeypatch.setattr(app_module, "FastMCP", FastMCPDuplo)

    async def _sem_dependencias(**_):
        return []

    monkeypatch.setattr(
        app_module, "health_registry", MagicMock(run_all=_sem_dependencias)
    )
    monkeypatch.setattr(app_module, "run_probe_loop", _laco_parado)

    bigquery = sys.modules["src.utils.bigquery"]

    async def _nada(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bigquery, "expirar_arquivos_dlq_async", _nada)
    monkeypatch.setattr(bigquery, "drain_bigquery_dlq_loop", _laco_parado)

    chamadas = []

    async def _aquecer():
        chamadas.append("aquecer_lineup")
        return True

    async def _refresh():
        chamadas.append("run_refresh_loop")
        await asyncio.Event().wait()

    monkeypatch.setattr(app_module, "aquecer_lineup", _aquecer)
    monkeypatch.setattr(app_module, "run_refresh_loop", _refresh)

    app_module.create_app()
    return construcao["lifespan"], chamadas


@pytest.mark.asyncio
async def test_lifespan_liga_o_line_up_quando_a_tool_esta_registrada(
    monkeypatch, importar_app_com_ambiente
):
    app_module = importar_app_com_ambiente(
        IS_LOCAL="false", EXCLUDED_TOOLS="user_feedback"
    )
    lifespan, chamadas = _lifespan_isolado(app_module, monkeypatch)

    async with lifespan(None):
        await asyncio.sleep(0)

    assert chamadas == ["aquecer_lineup", "run_refresh_loop"]


@pytest.mark.asyncio
async def test_lifespan_nao_bate_no_site_com_a_tool_excluida(
    monkeypatch, importar_app_com_ambiente
):
    """`EXCLUDED_TOOLS` precisa desligar também o trabalho de fundo.

    `conditional_mcp_tool` só controla o registro. Sem a guarda no lifespan, a
    tool sumia do catálogo e o laço seguia baixando as sete páginas do site de
    15 em 15 minutos, por réplica, para sempre — por ninguém.
    """
    app_module = importar_app_com_ambiente(
        IS_LOCAL="false", EXCLUDED_TOOLS="rock_in_rio_lineup"
    )
    lifespan, chamadas = _lifespan_isolado(app_module, monkeypatch)

    async with lifespan(None):
        await asyncio.sleep(0)

    assert chamadas == []
