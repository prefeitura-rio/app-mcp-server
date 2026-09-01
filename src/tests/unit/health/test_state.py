"""Testes de `src/health/state.py` — readiness dependente de dependência.

`evaluate_readiness()` combina o flag trivial de processo com uma sondagem
LIMITADA no tempo do Redis (a única dependência sem fallback em produção).
As garantias centrais:

- processo ainda subindo reprova sem gastar uma sondagem de Redis;
- Redis saudável libera tráfego; Redis indisponível reprova o pod;
- a sondagem nunca trava o probe: um Redis lento é cortado no teto
  configurado, não esperado até o fim;
- ambiente local (que não usa Redis como backend de estado) nunca sonda;
- nada aqui aciona o setup de tracing/métricas OTel — a leitura de
  readiness é independente da disponibilidade do coletor SigNoz.
"""

import asyncio
import sys
import time
import types

import pytest

from src.health import state
from src.health.models import HealthCheckError


@pytest.fixture(autouse=True)
def _restore_ready_state():
    original = state.is_ready()
    yield
    state.set_ready(original)


@pytest.fixture
def fake_env_prod(monkeypatch):
    env = types.SimpleNamespace(IS_LOCAL=False)
    monkeypatch.setattr(sys.modules["src.config"], "env", env, raising=False)
    monkeypatch.setitem(sys.modules, "src.config.env", env)
    return env


@pytest.fixture
def fake_env_local(monkeypatch):
    env = types.SimpleNamespace(IS_LOCAL=True)
    monkeypatch.setattr(sys.modules["src.config"], "env", env, raising=False)
    monkeypatch.setitem(sys.modules, "src.config.env", env)
    return env


def _install_fake_check_redis(monkeypatch, fn):
    fake_checks = types.ModuleType("src.health.checks")
    fake_checks.check_redis = fn
    monkeypatch.setitem(sys.modules, "src.health.checks", fake_checks)


@pytest.mark.asyncio
async def test_processo_nao_pronto_reprova_sem_gastar_sondagem_de_redis(
    fake_env_prod, monkeypatch
):
    state.set_ready(False)
    chamadas = []

    async def check_redis():
        chamadas.append(1)

    _install_fake_check_redis(monkeypatch, check_redis)

    ok, reason = await state.evaluate_readiness()

    assert (ok, reason) == (False, state.REASON_STARTING)
    assert chamadas == []


@pytest.mark.asyncio
async def test_ambiente_local_libera_trafego_sem_sondar_redis(fake_env_local):
    state.set_ready(True)

    ok, reason = await state.evaluate_readiness()

    assert (ok, reason) == (True, None)


@pytest.mark.asyncio
async def test_redis_saudavel_libera_trafego(fake_env_prod, monkeypatch):
    state.set_ready(True)

    async def check_redis():
        from src.health.models import CheckStatus

        return CheckStatus.UP

    _install_fake_check_redis(monkeypatch, check_redis)

    ok, reason = await state.evaluate_readiness()

    assert (ok, reason) == (True, None)


@pytest.mark.asyncio
async def test_redis_indisponivel_reprova_o_pod(fake_env_prod, monkeypatch):
    state.set_ready(True)

    async def check_redis():
        raise HealthCheckError("ping sem resposta")

    _install_fake_check_redis(monkeypatch, check_redis)

    ok, reason = await state.evaluate_readiness()

    assert (ok, reason) == (False, state.REASON_REDIS_UNAVAILABLE)


@pytest.mark.asyncio
async def test_checagem_de_redis_e_cortada_no_teto_configurado(
    fake_env_prod, monkeypatch
):
    state.set_ready(True)
    monkeypatch.setattr(state, "READINESS_REDIS_TIMEOUT_S", 0.05)

    async def check_redis():
        await asyncio.sleep(10)
        from src.health.models import CheckStatus

        return CheckStatus.UP

    _install_fake_check_redis(monkeypatch, check_redis)

    started = time.monotonic()
    ok, reason = await state.evaluate_readiness()
    elapsed = time.monotonic() - started

    assert (ok, reason) == (False, state.REASON_REDIS_UNAVAILABLE)
    assert elapsed < 1.0, "uma sondagem lenta não pode travar o probe de readiness"


def test_motivos_expostos_sao_um_conjunto_fechado_e_nao_sensivel():
    assert state.REASON_STARTING == "starting"
    assert state.REASON_REDIS_UNAVAILABLE == "redis_unavailable"


@pytest.mark.asyncio
async def test_readiness_nunca_aciona_setup_de_tracing_otel(fake_env_prod, monkeypatch):
    """Readiness precisa continuar correta mesmo com o coletor OTel fora do
    ar. A prova mais forte disso: o caminho de readiness nem chama o setup
    de tracing/métricas, então a disponibilidade do coletor SigNoz é
    irrelevante para o resultado — não apenas "tolerada"."""
    import src.observability.tracing as tracing

    def _explode():
        raise AssertionError("readiness não deveria acionar o setup de tracing/OTel")

    monkeypatch.setattr(tracing, "setup_tracing", _explode)

    state.set_ready(True)

    async def check_redis():
        from src.health.models import CheckStatus

        return CheckStatus.UP

    _install_fake_check_redis(monkeypatch, check_redis)

    ok, reason = await state.evaluate_readiness()

    assert (ok, reason) == (True, None)
