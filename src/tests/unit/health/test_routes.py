"""Testes das rotas de health (src/health/routes.py).

Duas garantias centrais verificadas aqui:

1. Uma dependência não-crítica fora do ar (BigQuery, Keycloak, ...) aparece
   em `/health/detail`, mas NÃO faz `/health` nem `/health/ready` retornarem
   erro — do contrário o kubelet mataria o pod (ou o tiraria do balanceador)
   por uma falha que quebra apenas parte das tools.
2. Redis fora do ar (a única dependência sem fallback em produção) faz
   `/health/ready` retornar 503 sem afetar `/health` — ver
   `src/health/state.py` para o porquê deste comportamento mudou na Task 7.
"""

import json
import sys
import types
from unittest.mock import AsyncMock

import pytest

from src.health import routes, state
from src.health.models import CheckStatus, HealthCheckError
from src.health.registry import HealthRegistry


@pytest.fixture(autouse=True)
def fake_env(monkeypatch):
    env = types.SimpleNamespace(ENVIRONMENT="test", IS_LOCAL=True)
    monkeypatch.setattr(sys.modules["src.config"], "env", env, raising=False)
    monkeypatch.setitem(sys.modules, "src.config.env", env)
    return env


@pytest.fixture
def registry(monkeypatch):
    registry = HealthRegistry(cache_ttl_s=0.0)
    monkeypatch.setattr(routes, "health_registry", registry)
    return registry


@pytest.fixture(autouse=True)
def _restore_ready_state():
    original = state.is_ready()
    yield
    state.set_ready(original)


def _body(response) -> dict:
    return json.loads(response.body)


@pytest.mark.asyncio
async def test_health_e_sempre_ok():
    response = await routes.health(None)

    assert response.status_code == 200
    assert response.body == b"OK"


@pytest.mark.asyncio
async def test_ready_reflete_o_estado_do_processo():
    """Ambiente local (`fake_env.IS_LOCAL=True`) não sonda Redis — só o flag
    de processo é relevante aqui. O caminho de sondagem tem cobertura própria
    em `src/tests/unit/health/test_state.py`."""
    state.set_ready(False)
    body = _body(await routes.ready(None))
    assert body == {"status": "not_ready", "reason": "starting"}

    state.set_ready(True)
    assert _body(await routes.ready(None)) == {"status": "ready"}


@pytest.mark.asyncio
async def test_ready_retorna_503_com_motivo_quando_redis_esta_fora(monkeypatch):
    state.set_ready(True)
    monkeypatch.setattr(
        routes,
        "evaluate_readiness",
        AsyncMock(return_value=(False, "redis_unavailable")),
    )

    response = await routes.ready(None)

    assert response.status_code == 503
    assert _body(response) == {"status": "not_ready", "reason": "redis_unavailable"}


@pytest.mark.asyncio
async def test_detail_reporta_ok_quando_tudo_esta_de_pe(registry):
    async def up() -> CheckStatus:
        return CheckStatus.UP

    registry.register("redis", up)

    response = await routes.detail(None)
    body = _body(response)

    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["checks"][0]["name"] == "redis"


@pytest.mark.asyncio
async def test_detail_reporta_degraded_sem_nunca_retornar_503(registry):
    async def down() -> CheckStatus:
        raise HealthCheckError("ping sem resposta")

    async def up() -> CheckStatus:
        return CheckStatus.UP

    registry.register("redis", down, critical=True)
    registry.register("data_files", up)

    response = await routes.detail(None)
    body = _body(response)

    # 200 mesmo degradado: é diagnóstico, não probe.
    assert response.status_code == 200
    assert body["status"] == "degraded"

    por_nome = {c["name"]: c for c in body["checks"]}
    assert por_nome["redis"]["status"] == "down"
    assert por_nome["data_files"]["status"] == "up"


@pytest.mark.asyncio
async def test_check_pulado_nao_degrada_o_agregado(registry):
    async def skipped() -> CheckStatus:
        return CheckStatus.SKIPPED

    registry.register("keycloak_jwks", skipped)

    body = _body(await routes.detail(None))

    assert body["status"] == "ok"


@pytest.mark.asyncio
async def test_dependencia_nao_critica_fora_nao_derruba_liveness_nem_readiness(
    registry,
):
    """BigQuery/Keycloak/etc. só aparecem em `/health/detail`: sua queda
    degrada uma tool específica, não o processo inteiro, então não devem
    gatear tráfego. Isso é diferente do Redis, que passou a gatear
    `/health/ready` a partir da Task 7 — ver `test_state.py`."""

    async def down() -> CheckStatus:
        raise ConnectionError("Error 111 connecting to bigquery.googleapis.com")

    registry.register("bigquery", down, critical=True)
    state.set_ready(True)

    assert _body(await routes.detail(None))["status"] == "degraded"
    assert (await routes.health(None)).status_code == 200
    # `IS_LOCAL=True` (fake_env) faz a readiness ignorar Redis por completo;
    # o ponto aqui é que o check registrado acima (não-Redis) nunca é
    # sequer consultado pela readiness.
    assert (await routes.ready(None)).status_code == 200


@pytest.mark.asyncio
async def test_payload_nao_vaza_credencial_nem_host(registry):
    async def down() -> CheckStatus:
        raise ConnectionError(
            "Error 111 connecting to redis://:senha-secreta@10.0.0.1:6379/0"
        )

    registry.register("redis", down)

    raw = (await routes.detail(None)).body.decode()

    assert "senha-secreta" not in raw
    assert "10.0.0.1" not in raw
    assert "redis://" not in raw
    assert "ConnectionError" in raw  # o operador ainda sabe o que aconteceu


@pytest.mark.asyncio
async def test_detail_traz_metadados_de_operacao(registry):
    body = _body(await routes.detail(None))

    assert body["environment"] == "test"
    assert "version" in body
    assert isinstance(body["uptime_s"], int)
    assert isinstance(body["ready"], bool)


def test_register_health_routes_registra_as_tres_rotas():
    registradas = []

    class FakeMcp:
        def custom_route(self, path, methods):
            registradas.append((path, tuple(methods)))
            return lambda fn: fn

    routes.register_health_routes(FakeMcp())

    assert registradas == [
        ("/health", ("GET",)),
        ("/health/ready", ("GET",)),
        ("/health/detail", ("GET",)),
    ]
