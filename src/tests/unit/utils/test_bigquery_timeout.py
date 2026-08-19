"""Timeout e não-bloqueio do event loop nas leituras do BigQuery (CHATR-125).

O critério de aceite da subtarefa é "timeout configurável e testado", e a parte
testada faltava: o handler de `asyncio.TimeoutError` em `_run_query_com_timeout`
nunca era executado pela suíte.

São dois mecanismos empilhados, e os testes separam os dois:

* `asyncio.wait_for` limita a *espera* do `await` — é ele que impede o tool call
  de ficar preso, e é o caminho que produz `TimeoutError`.
* `query_job.result(timeout=...)` limita a thread do executor. Sem ele o `await`
  seria liberado mas a thread continuaria ocupada até a query terminar sozinha.
"""

import asyncio
import base64
import importlib.util
import json
import sys
import threading
import time
import types
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[4]


def _ensure_package(name: str, path: Path) -> None:
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(path)]
    sys.modules[name] = pkg


def _passthrough_interceptor(*_args, **_kwargs):
    def decorator(func):
        return func

    return decorator


def _load_bigquery_module(monkeypatch, alias: str, **env_extra):
    _ensure_package("src", PROJECT_ROOT / "src")
    _ensure_package("src.config", PROJECT_ROOT / "src" / "config")
    _ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")

    valores = {
        "GCP_SERVICE_ACCOUNT_CREDENTIALS": base64.b64encode(
            json.dumps({"project_id": "proj-timeout-test"}).encode()
        ).decode(),
        "GOOGLE_BIGQUERY_PAGE_SIZE": 100,
        "BIGQUERY_TIMEOUT_SECONDS": 10.0,
        "BIGQUERY_CACHE_TTL_SECONDS": 3600,
    }
    valores.update(env_extra)  # o teste sobrescreve o default, não colide com ele
    env_module = types.SimpleNamespace(**valores)
    monkeypatch.setitem(sys.modules, "src.config.env", env_module)
    monkeypatch.setitem(
        sys.modules, "src.config", types.SimpleNamespace(env=env_module)
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.error_interceptor",
        types.SimpleNamespace(interceptor=_passthrough_interceptor),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.log",
        types.SimpleNamespace(
            logger=types.SimpleNamespace(
                info=lambda *_a, **_k: None,
                error=lambda *_a, **_k: None,
                warning=lambda *_a, **_k: None,
                exception=lambda *_a, **_k: None,
            )
        ),
    )

    spec = importlib.util.spec_from_file_location(
        alias, PROJECT_ROOT / "src" / "utils" / "bigquery.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _RedisFalso:
    def __init__(self):
        self.store = {}
        self.setex_calls = []

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.setex_calls.append((key, ttl, value))
        self.store[key] = value


class _ClienteLento:
    """Query que demora `delay` segundos dentro da thread do executor."""

    def __init__(self, delay: float):
        self.delay = delay
        self.kwargs_do_result = []
        self.terminou = threading.Event()

    def query(self, _query, **_kwargs):
        outer = self

        class Job:
            def result(self, page_size=None, timeout=None):
                outer.kwargs_do_result.append(
                    {"page_size": page_size, "timeout": timeout}
                )
                time.sleep(outer.delay)
                outer.terminou.set()
                return []

        return Job()


def _montar(monkeypatch, alias, delay=1.0, redis=None, **env_extra):
    module = _load_bigquery_module(monkeypatch, alias, **env_extra)
    cliente = _ClienteLento(delay)
    monkeypatch.setattr(module, "get_bigquery_client", lambda: cliente)
    redis = redis if redis is not None else _RedisFalso()

    async def _redis_falso():
        return redis

    monkeypatch.setattr(module, "get_async_redis_client", _redis_falso)
    return module, cliente, redis


# ---------------------------------------------------------------------------
# O handler propriamente dito
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_lenta_levanta_timeout_error(monkeypatch):
    """Uma query lenta não pode prender o tool call indefinidamente."""
    module, _cliente, _redis = _montar(monkeypatch, "bq_timeout_dispara", delay=1.0)

    with pytest.raises(TimeoutError) as exc:
        await module.get_bigquery_result(
            "select 1", cache_ttl_seconds=0, timeout_seconds=0.2
        )

    assert "timed out after 0.2s" in str(exc.value)


@pytest.mark.asyncio
async def test_timeout_dispara_no_prazo_e_nao_espera_a_query(monkeypatch):
    """O prazo é do `await`: não adianta abortar depois que a query terminou."""
    module, _cliente, _redis = _montar(monkeypatch, "bq_timeout_prazo", delay=1.0)

    inicio = time.monotonic()
    with pytest.raises(TimeoutError):
        await module.get_bigquery_result(
            "select 1", cache_ttl_seconds=0, timeout_seconds=0.2
        )
    decorrido = time.monotonic() - inicio

    assert decorrido < 0.8, f"abortou em {decorrido:.2f}s, esperado ~0.2s"


@pytest.mark.asyncio
async def test_query_dentro_do_prazo_nao_estoura(monkeypatch):
    """Controle: o timeout não pode disparar em query que respondeu a tempo."""
    module, _cliente, _redis = _montar(monkeypatch, "bq_timeout_ok", delay=0.05)

    assert (
        await module.get_bigquery_result(
            "select 1", cache_ttl_seconds=0, timeout_seconds=5
        )
        == []
    )


# ---------------------------------------------------------------------------
# Configurabilidade — a outra metade do critério de aceite
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_por_chamada_tem_precedencia_sobre_o_env(monkeypatch):
    module, _cliente, _redis = _montar(
        monkeypatch, "bq_timeout_param", delay=0.8, BIGQUERY_TIMEOUT_SECONDS=30.0
    )

    with pytest.raises(TimeoutError) as exc:
        await module.get_bigquery_result(
            "select 1", cache_ttl_seconds=0, timeout_seconds=0.2
        )

    assert "0.2s" in str(exc.value)


@pytest.mark.asyncio
async def test_timeout_padrao_vem_do_env(monkeypatch):
    """Sem `timeout_seconds`, vale `BIGQUERY_TIMEOUT_SECONDS`."""
    module, _cliente, _redis = _montar(
        monkeypatch, "bq_timeout_env", delay=0.8, BIGQUERY_TIMEOUT_SECONDS=0.2
    )

    with pytest.raises(TimeoutError) as exc:
        await module.get_bigquery_result("select 1", cache_ttl_seconds=0)

    assert "0.2s" in str(exc.value)


@pytest.mark.asyncio
async def test_timeout_e_repassado_ao_result_do_bigquery(monkeypatch):
    """Sem isso a thread do executor ficaria ocupada até a query acabar sozinha.

    O `asyncio.wait_for` libera o `await`, mas não cancela a thread — quem
    limita a thread é o timeout do próprio `.result()`.
    """
    module, cliente, _redis = _montar(monkeypatch, "bq_timeout_repasse", delay=0.01)

    await module.get_bigquery_result(
        "select 1", cache_ttl_seconds=0, timeout_seconds=7.5
    )

    assert cliente.kwargs_do_result[0]["timeout"] == 7.5


# ---------------------------------------------------------------------------
# Não bloquear o event loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_loop_continua_rodando_durante_a_query(monkeypatch):
    """A query é síncrona; se rodasse no loop, travaria o processo inteiro."""
    module, _cliente, _redis = _montar(monkeypatch, "bq_timeout_loop", delay=0.5)

    batidas = 0

    async def relogio():
        nonlocal batidas
        while True:
            await asyncio.sleep(0.02)
            batidas += 1

    tarefa = asyncio.create_task(relogio())
    await module.get_bigquery_result("select 1", cache_ttl_seconds=0, timeout_seconds=5)
    tarefa.cancel()

    assert batidas >= 10, f"apenas {batidas} batidas em 0.5s — o loop travou"


@pytest.mark.asyncio
async def test_outras_tarefas_avancam_enquanto_uma_espera_o_timeout(monkeypatch):
    """Durante o timeout de uma requisição, as outras não podem ficar reféns."""
    module, _cliente, _redis = _montar(monkeypatch, "bq_timeout_loop_2", delay=0.8)

    async def travada():
        with pytest.raises(TimeoutError):
            await module.get_bigquery_result(
                "select lenta", cache_ttl_seconds=0, timeout_seconds=0.3
            )
        return "estourou"

    async def rapida():
        await asyncio.sleep(0.05)
        return "seguiu"

    resultados = await asyncio.gather(travada(), rapida())

    assert resultados == ["estourou", "seguiu"]


# ---------------------------------------------------------------------------
# O que o timeout não pode deixar para trás
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_nao_grava_nada_no_cache(monkeypatch):
    """Query que não terminou não tem resultado: nada a cachear."""
    module, _cliente, redis = _montar(monkeypatch, "bq_timeout_sem_cache", delay=0.8)

    with pytest.raises(TimeoutError):
        await module.get_bigquery_result(
            "select 1", cache_ttl_seconds=3600, timeout_seconds=0.2
        )

    assert redis.setex_calls == []


@pytest.mark.asyncio
async def test_timeout_libera_o_lock_de_single_flight(monkeypatch):
    """Chave travada para sempre transformaria um timeout em pane permanente."""
    module, _cliente, _redis = _montar(monkeypatch, "bq_timeout_lock", delay=0.8)

    with pytest.raises(TimeoutError):
        await module.get_bigquery_result(
            "select 1", cache_ttl_seconds=3600, timeout_seconds=0.2
        )

    assert module._inflight_locks == {}
    assert module._inflight_refs == {}


@pytest.mark.asyncio
async def test_apos_timeout_a_chave_volta_a_ser_consultavel(monkeypatch):
    """A requisição seguinte tem de conseguir tentar de novo."""
    module = _load_bigquery_module(monkeypatch, "bq_timeout_recupera")
    redis = _RedisFalso()

    async def _redis_falso():
        return redis

    monkeypatch.setattr(module, "get_async_redis_client", _redis_falso)

    monkeypatch.setattr(module, "get_bigquery_client", lambda: _ClienteLento(0.8))
    with pytest.raises(TimeoutError):
        await module.get_bigquery_result(
            "select 1", cache_ttl_seconds=3600, timeout_seconds=0.2
        )

    monkeypatch.setattr(module, "get_bigquery_client", lambda: _ClienteLento(0.01))
    assert (
        await module.get_bigquery_result(
            "select 1", cache_ttl_seconds=3600, timeout_seconds=5
        )
        == []
    )
    assert len(redis.setex_calls) == 1
