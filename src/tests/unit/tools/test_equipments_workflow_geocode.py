"""Regressão para o caminho de geocodificação do workflow de equipamentos.

Este caminho (pontos de apoio) não tinha nenhuma cobertura e usava
`requests.get` síncrono dentro de um nó `async`, bloqueando o event loop por
até 10s por chamada. Os testes abaixo travam as duas garantias: a função é
assíncrona e delega para a geocodificação async do projeto.
"""

import ast
import asyncio
import inspect

import pytest

from src.tools.multi_step_service.core.models import ServiceState
from src.tools.multi_step_service.workflows.equipments import (
    equipments_workflow as workflow_module,
)


def test_geocode_helper_is_async():
    """Se alguém reverter para uma versão síncrona, este teste quebra."""
    assert inspect.iscoroutinefunction(
        workflow_module._geocode_and_extract_neighborhood
    )


def test_workflow_module_does_not_import_requests():
    """O projeto inteiro usa httpx async; `requests` aqui significa bloqueio.

    Percorre a AST em vez do texto para não confundir menção em docstring com
    import de verdade, e para pegar também import dentro de função.
    """
    tree = ast.parse(inspect.getsource(workflow_module))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "requests" not in imported


@pytest.mark.asyncio
async def test_geocode_returns_normalized_neighborhood(monkeypatch):
    async def fake_get_coordinates_google(address):
        assert address == "Rua Qualquer, 100"
        return {"lat": -22.9, "lng": -43.2, "bairro_normalizado": "acari"}

    monkeypatch.setattr(
        workflow_module, "get_coordinates_google", fake_get_coordinates_google
    )

    assert (
        await workflow_module._geocode_and_extract_neighborhood("Rua Qualquer, 100")
        == "acari"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"bairro_normalizado": ""}])
async def test_geocode_returns_none_when_neighborhood_missing(monkeypatch, payload):
    """Falha de geocodificação vira `None`, nunca string vazia."""

    async def fake_get_coordinates_google(address):
        return payload

    monkeypatch.setattr(
        workflow_module, "get_coordinates_google", fake_get_coordinates_google
    )

    assert await workflow_module._geocode_and_extract_neighborhood("Rua X") is None


@pytest.mark.asyncio
async def test_geocode_does_not_block_the_event_loop(monkeypatch):
    """O loop precisa continuar servindo outras tarefas durante a geocodificação.

    Com a versão síncrona (`requests.get`) o ticker abaixo não avançaria
    nenhuma vez enquanto a chamada estivesse em voo.
    """
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.001)
            ticks += 1

    async def slow_get_coordinates_google(address):
        await asyncio.sleep(0.05)
        return {"bairro_normalizado": "guaratiba"}

    monkeypatch.setattr(
        workflow_module, "get_coordinates_google", slow_get_coordinates_google
    )

    ticker_task = asyncio.create_task(ticker())
    try:
        result = await workflow_module._geocode_and_extract_neighborhood("Rua Y")
    finally:
        ticker_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await ticker_task

    assert result == "guaratiba"
    assert ticks > 0, "event loop ficou bloqueado durante a geocodificação"


@pytest.mark.asyncio
async def test_search_node_awaits_geocode_and_blocks_disallowed_neighborhood(
    monkeypatch,
):
    """O nó async precisa dar `await` no helper — sem isso viria uma coroutine.

    Uma coroutine nunca está em `ALLOWED_NEIGHBORHOODS_PONTOS_APOIO`, então o
    bug apareceria como "bairro sempre negado" em vez de erro explícito.
    """
    calls = []

    async def fake_get_coordinates_google(address):
        calls.append(address)
        return {"bairro_normalizado": "copacabana"}

    monkeypatch.setattr(
        workflow_module, "get_coordinates_google", fake_get_coordinates_google
    )

    state = ServiceState(
        user_id="5521999999999",
        service_name="equipments_search",
        payload={"address": "Rua Z, 1", "categories": ["PONTOS_DE_APOIO"]},
        data={},
    )
    result = await workflow_module.EquipmentsWorkflow()._search_equipments(state)

    assert calls == ["Rua Z, 1"]
    assert result.agent_response is not None
    assert "199" in result.agent_response.description


@pytest.mark.asyncio
async def test_search_node_allows_whitelisted_neighborhood(monkeypatch):
    """Bairro na whitelist não pode ser barrado pela checagem de pontos de apoio."""

    async def fake_get_coordinates_google(address):
        return {"bairro_normalizado": "acari"}

    async def fake_get_equipments_with_instructions(**kwargs):
        return {"equipamentos": []}

    monkeypatch.setattr(
        workflow_module, "get_coordinates_google", fake_get_coordinates_google
    )
    monkeypatch.setattr(
        workflow_module,
        "get_equipments_with_instructions",
        fake_get_equipments_with_instructions,
    )

    state = ServiceState(
        user_id="5521999999999",
        service_name="equipments_search",
        payload={"address": "Rua Acari, 1", "categories": ["PONTOS_DE_APOIO"]},
        data={},
    )
    result = await workflow_module.EquipmentsWorkflow()._search_equipments(state)

    assert result.agent_response is None or "199" not in (
        result.agent_response.description or ""
    )
