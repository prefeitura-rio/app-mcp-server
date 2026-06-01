"""Testes do reset_session_state (encerramento de sessão — limpa o StateManager)."""

import pytest

from src.tools import langgraph_workflows


@pytest.mark.asyncio
async def test_reset_session_state_clears_user_workflow(monkeypatch):
    """Chama StateManager.remove_user_data() com o user_id do thread e devolve
    status/cleared. O user_id chega já resolvido (o engine sobrescreve qualquer
    valor do modelo pelo thread_id autenticado antes da execução)."""
    captured = {}

    class _FakeStateManager:
        def __init__(self, user_id, backend_mode=None):
            captured["user_id"] = user_id
            captured["backend_mode"] = backend_mode

        async def remove_user_data(self):
            captured["removed"] = True
            return True

    monkeypatch.setattr(langgraph_workflows, "StateManager", _FakeStateManager)

    out = await langgraph_workflows.reset_session_state("5521999998888")

    assert captured["removed"] is True
    assert captured["user_id"] == "5521999998888"
    assert out == {"status": "ok", "cleared": True}


@pytest.mark.asyncio
async def test_reset_session_state_reports_noop_when_nothing_to_clear(monkeypatch):
    """Sem estado pra limpar, remove_user_data devolve False → cleared=False."""

    class _FakeStateManager:
        def __init__(self, user_id, backend_mode=None):
            pass

        async def remove_user_data(self):
            return False

    monkeypatch.setattr(langgraph_workflows, "StateManager", _FakeStateManager)

    out = await langgraph_workflows.reset_session_state("5521999998888")
    assert out == {"status": "ok", "cleared": False}


@pytest.mark.asyncio
async def test_reset_session_state_returns_error_on_backend_failure(monkeypatch):
    """Falha do backend (ex.: blip do Redis em produção) vira erro ESTRUTURADO,
    não exceção propagada — segue a convenção catch-and-degrade do repo, então o
    encerramento não estoura ToolError pro modelo."""

    class _FailingStateManager:
        def __init__(self, user_id, backend_mode=None):
            pass

        async def remove_user_data(self):
            raise RuntimeError("redis indisponível")

    monkeypatch.setattr(langgraph_workflows, "StateManager", _FailingStateManager)

    out = await langgraph_workflows.reset_session_state("5521999998888")
    assert out == {"status": "error", "cleared": False}


@pytest.mark.asyncio
async def test_reset_session_state_handles_construction_failure(monkeypatch):
    """A falha pode vir já na CONSTRUÇÃO do StateManager (backend Redis com config
    inválida/indisponível), antes do delete — também deve degradar pra erro
    estruturado, não propagar ToolError."""

    class _ExplodingStateManager:
        def __init__(self, user_id, backend_mode=None):
            raise RuntimeError("backend Redis indisponível")

    monkeypatch.setattr(langgraph_workflows, "StateManager", _ExplodingStateManager)

    out = await langgraph_workflows.reset_session_state("5521999998888")
    assert out == {"status": "error", "cleared": False}
