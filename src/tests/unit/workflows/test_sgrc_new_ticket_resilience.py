"""Testes do timeout (#R2) do callout SGRC em `SGRCTicketMixin.new_ticket`.

Cobre: sucesso, timeout limitado (fail-fast, sem re-POST) e propagação de erro
(nada é engolido). `asyncio_mode = "auto"` no pyproject dispensa o marker asyncio.

Nota: patchamos os atributos em ``sgrc_mod.env`` (o objeto que `new_ticket` lê)
com ``raising=False`` — a suíte completa instala um ``MockEnv`` como
``src.config.env`` (conftest do interceptor) que não tem ``SGRC_TIMEOUT_SECONDS``.
"""

import asyncio
import types

import pytest
from prefeitura_rio.integrations.sgrc.exceptions import SGRCInternalErrorException

from src.tools.multi_step_service.workflows.sgrc_components import sgrc as sgrc_mod
from src.tools.multi_step_service.workflows.sgrc_components.sgrc import SGRCTicketMixin


def _mixin():
    """Instância mínima do mixin — `new_ticket` não usa atributos de self."""
    return type("_T", (SGRCTicketMixin,), {})()


def _fake_ticket(pid: str = "RIO-TEST-1"):
    return types.SimpleNamespace(protocol_id=pid)


async def test_success_returns_ticket(monkeypatch):
    monkeypatch.setattr(sgrc_mod.env, "SGRC_TIMEOUT_SECONDS", 5.0, raising=False)

    async def ok(**_kwargs):
        return _fake_ticket()

    monkeypatch.setattr(sgrc_mod, "async_new_ticket", ok)

    ticket = await _mixin().new_ticket(classification_code="1607")

    assert ticket.protocol_id == "RIO-TEST-1"


async def test_timeout_is_bounded_and_not_retried(monkeypatch):
    # O timeout limita a chamada (mata o pendura de 300s) e é fail-fast: uma
    # única tentativa, sem re-POST (evita duplicar o chamado).
    monkeypatch.setattr(sgrc_mod.env, "SGRC_TIMEOUT_SECONDS", 0.05, raising=False)
    calls = {"n": 0}

    async def hangs(**_kwargs):
        calls["n"] += 1
        await asyncio.sleep(1.0)  # excede o timeout de 0.05s

    monkeypatch.setattr(sgrc_mod, "async_new_ticket", hangs)

    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await _mixin().new_ticket(classification_code="1607")

    assert calls["n"] == 1  # fail-fast: uma única tentativa


async def test_error_propagates_not_swallowed(monkeypatch):
    # Erro do SGRC propaga pro handler de _open_ticket (que classifica e preserva
    # o estado) — `new_ticket` não engole a exceção.
    monkeypatch.setattr(sgrc_mod.env, "SGRC_TIMEOUT_SECONDS", 5.0, raising=False)
    calls = {"n": 0}

    async def boom(**_kwargs):
        calls["n"] += 1
        raise SGRCInternalErrorException("500 do SGRC")

    monkeypatch.setattr(sgrc_mod, "async_new_ticket", boom)

    with pytest.raises(SGRCInternalErrorException):
        await _mixin().new_ticket(classification_code="1607")

    assert calls["n"] == 1  # sem retry interno
