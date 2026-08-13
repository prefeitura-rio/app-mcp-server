"""Testes do executor de checks (src/health/registry.py)."""

import asyncio

import pytest

from src.health.models import CheckStatus, HealthCheckError
from src.health.registry import HealthRegistry, sanitize_error


def _registry(**kwargs) -> HealthRegistry:
    defaults = {"cache_ttl_s": 0.0, "global_timeout_s": 5.0, "default_timeout_s": 1.0}
    return HealthRegistry(**{**defaults, **kwargs})


async def _up() -> CheckStatus:
    return CheckStatus.UP


@pytest.mark.asyncio
async def test_check_bem_sucedido_vira_up():
    registry = _registry()
    registry.register("ok", _up)

    (result,) = await registry.run_all()

    assert result.name == "ok"
    assert result.status is CheckStatus.UP
    assert result.error is None


@pytest.mark.asyncio
async def test_excecao_em_um_check_nao_afeta_os_demais():
    async def explode() -> CheckStatus:
        raise RuntimeError("boom")

    registry = _registry()
    registry.register("bom", _up)
    registry.register("ruim", explode)

    results = {r.name: r for r in await registry.run_all()}

    assert results["bom"].status is CheckStatus.UP
    assert results["ruim"].status is CheckStatus.DOWN


@pytest.mark.asyncio
async def test_check_lento_respeita_o_timeout():
    async def lento() -> CheckStatus:
        await asyncio.sleep(10)
        return CheckStatus.UP

    registry = _registry()
    registry.register("lento", lento, timeout_s=0.05)

    (result,) = await registry.run_all()

    assert result.status is CheckStatus.DOWN
    assert result.error == "timeout"
    assert result.latency_ms < 1000


@pytest.mark.asyncio
async def test_retorno_invalido_vira_down():
    async def confuso():
        return "provavelmente ok"

    registry = _registry()
    registry.register("confuso", confuso)

    (result,) = await registry.run_all()

    assert result.status is CheckStatus.DOWN
    assert result.error == "invalid_check_result"


@pytest.mark.asyncio
async def test_resultado_e_reaproveitado_dentro_do_ttl():
    execucoes = []

    async def contando() -> CheckStatus:
        execucoes.append(1)
        return CheckStatus.UP

    registry = _registry(cache_ttl_s=60.0)
    registry.register("contando", contando)

    await registry.run_all()
    await registry.run_all()
    await registry.run_all()
    assert len(execucoes) == 1, "TTL deve proteger a dependência de rajadas"

    await registry.run_all(force=True)
    assert len(execucoes) == 2, "force deve ignorar o cache"


@pytest.mark.asyncio
async def test_rajada_concorrente_dispara_uma_unica_rodada():
    execucoes = []

    async def contando() -> CheckStatus:
        execucoes.append(1)
        await asyncio.sleep(0.01)
        return CheckStatus.UP

    registry = _registry(cache_ttl_s=60.0)
    registry.register("contando", contando)

    await asyncio.gather(*(registry.run_all() for _ in range(10)))

    assert len(execucoes) == 1


@pytest.mark.asyncio
async def test_teto_global_encerra_a_rodada():
    async def bloqueante() -> CheckStatus:
        await asyncio.sleep(10)
        return CheckStatus.UP

    # Timeout por check maior que o teto global: só o teto pode salvar.
    registry = _registry(global_timeout_s=0.05)
    registry.register("bloqueante", bloqueante, timeout_s=30.0)

    (result,) = await registry.run_all()

    assert result.status is CheckStatus.DOWN
    assert result.error == "timeout"


@pytest.mark.asyncio
async def test_registry_vazio_devolve_lista_vazia():
    assert await _registry().run_all() == []


def test_sanitize_expoe_apenas_o_nome_da_classe():
    exc = ConnectionError("Error 111 connecting to redis-master.default:6379")
    assert sanitize_error(exc) == "ConnectionError"


def test_sanitize_nao_vaza_credencial_de_url():
    exc = ValueError("invalid url redis://:senha-super-secreta@10.0.0.1:6379/0")
    sanitized = sanitize_error(exc)
    assert "senha-super-secreta" not in sanitized
    assert "10.0.0.1" not in sanitized


def test_sanitize_preserva_mensagem_escrita_por_nos():
    assert sanitize_error(HealthCheckError("ping sem resposta")) == "ping sem resposta"


def test_sanitize_traduz_timeout():
    assert sanitize_error(asyncio.TimeoutError()) == "timeout"
