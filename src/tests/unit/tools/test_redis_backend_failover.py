"""Resiliência do `RedisBackend` à janela de failover do Sentinel/HAProxy.

`RedisBackend` (src/tools/multi_step_service/core/state.py) fala com o
`mcp-redis` do infra/superapp: um endpoint HAProxy estável na frente de um
Sentinel que promove uma réplica quando o primary cai (Task 5 do plano de
resiliência). Durante a janela entre a queda e a promoção, chamadas ao
cliente Redis recebem `redis.ConnectionError` mesmo sem o `REDIS_URL` mudar.

Estes testes fixam dois comportamentos com um cliente Redis dublê
determinístico — sem Redis de verdade, sem `asyncio.sleep` real:

1. Caracterização do que já funcionava (não pode regredir): operação sem
   falha alguma, e um erro que não é `ConnectionError` (não se beneficia de
   retry) propaga na primeira tentativa.
2. O novo contrato do Task 6: uma falha transitória de conexão se recupera
   dentro do orçamento de retry, e uma falha persistente esgota o orçamento
   e relança o `redis.ConnectionError` original — nunca o esconde nem o
   troca de tipo.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest
import redis


PROJECT_ROOT = Path(__file__).resolve().parents[4]
STATE_MODULE_PATH = (
    PROJECT_ROOT / "src" / "tools" / "multi_step_service" / "core" / "state.py"
)
REDIS_URL = "redis://:pw@mcp-redis:6379/0"


def _load_state_module(monkeypatch):
    """Carrega `state.py` isolado, sem puxar a árvore de modelos Pydantic.

    `RedisBackend` não usa `ServiceState`/`ServiceMetadata`; um stub simples
    basta para o import de `src.tools.multi_step_service.core.models` não
    quebrar.
    """
    env_stub = types.SimpleNamespace(REDIS_URL=REDIS_URL, REDIS_TTL_SECONDS=3600)
    monkeypatch.setitem(sys.modules, "src.config", types.SimpleNamespace(env=env_stub))
    monkeypatch.setitem(sys.modules, "src.config.env", env_stub)
    monkeypatch.setitem(
        sys.modules,
        "src.tools.multi_step_service.core.models",
        types.SimpleNamespace(ServiceState=object, ServiceMetadata=object),
    )

    spec = importlib.util.spec_from_file_location(
        "test_state_redis_failover_module", STATE_MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeRedisClient:
    """Dublê do `redis.asyncio.Redis`: cada chamada consome um desfecho.

    `outcomes` é a lista de desfechos, na ordem das chamadas (uma exceção
    ou um valor de retorno); quando as chamadas passam do fim da lista, o
    último desfecho se repete — útil para simular falha persistente sem
    listar dezenas de erros.
    """

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    async def _next(self):
        self.calls += 1
        outcome = self._outcomes[min(self.calls - 1, len(self._outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def get(self, _key):
        return await self._next()

    async def set(self, **_kwargs):
        return await self._next()

    async def delete(self, _key):
        return await self._next()

    async def ping(self):
        return await self._next()


@pytest.fixture
def state_module(monkeypatch):
    return _load_state_module(monkeypatch)


@pytest.fixture
def no_sleep(monkeypatch, state_module):
    """Substitui `asyncio.sleep` por um recorder: nenhum teste espera de verdade."""
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(state_module.asyncio, "sleep", fake_sleep)
    return slept


def _backend(state_module, outcomes, **retry_kwargs):
    backend = state_module.RedisBackend(
        redis_url=REDIS_URL, ttl_seconds=60, **retry_kwargs
    )
    backend.client = FakeRedisClient(outcomes)
    return backend


# ---------------------------------------------------------------------------
# Caracterização: comportamento que já existia e não pode regredir.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operacao_sem_falha_nao_retenta(state_module, no_sleep):
    """Caminho feliz: uma chamada, sem sleep, sem retry."""
    backend = _backend(state_module, ['{"a": 1}'])

    result = await backend.load_user_data("user-1")

    assert result == {"a": 1}
    assert backend.client.calls == 1
    assert no_sleep == []


@pytest.mark.asyncio
async def test_erro_que_nao_e_connection_error_propaga_na_primeira_tentativa(
    state_module, no_sleep
):
    """Um erro que retry não resolve (ex.: resposta malformada) não é retentado."""
    backend = _backend(state_module, [redis.ResponseError("WRONGTYPE")])

    with pytest.raises(redis.ResponseError):
        await backend.load_user_data("user-1")

    assert backend.client.calls == 1
    assert no_sleep == []


@pytest.mark.asyncio
async def test_health_check_retorna_false_em_erro_nao_de_conexao(
    state_module, no_sleep
):
    backend = _backend(state_module, [redis.ResponseError("erro qualquer")])

    assert await backend.health_check() is False
    assert backend.client.calls == 1


# ---------------------------------------------------------------------------
# Task 6: recuperação e esgotamento do orçamento de retry.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recupera_apos_connection_errors_transitorios(state_module, no_sleep):
    """Duas quedas seguidas do primary, terceira tentativa já no promovido."""
    backend = _backend(
        state_module,
        [
            redis.ConnectionError("Error 111 connecting to mcp-redis:6379"),
            redis.ConnectionError("Error 111 connecting to mcp-redis:6379"),
            '{"user_id": "42"}',
        ],
    )

    result = await backend.load_user_data("user-1")

    assert result == {"user_id": "42"}
    assert backend.client.calls == 3
    # Backoff exponencial sem jitter: 1ª e 2ª falhas esperam base*2^0 e base*2^1.
    assert no_sleep == [
        state_module.RedisBackend.DEFAULT_BASE_DELAY_SECONDS,
        state_module.RedisBackend.DEFAULT_BASE_DELAY_SECONDS * 2,
    ]


@pytest.mark.asyncio
async def test_esgotamento_relanca_o_connection_error_original(state_module, no_sleep):
    """Falha persistente: o orçamento acaba e o erro típico de sempre propaga."""
    erro = redis.ConnectionError("Error 111 connecting to mcp-redis:6379")
    backend = _backend(state_module, [erro], max_attempts=4, base_delay_seconds=0.1)

    with pytest.raises(redis.ConnectionError) as excinfo:
        await backend.load_user_data("user-1")

    assert excinfo.value is erro
    assert backend.client.calls == 4
    # 3 esperas entre as 4 tentativas; nenhuma depois da última (já desistiu).
    assert no_sleep == [0.1, 0.2, 0.4]


@pytest.mark.asyncio
async def test_esgotamento_no_health_check_continua_retornando_false(
    state_module, no_sleep
):
    """`health_check` já engolia qualquer exceção: continua assim após o retry."""
    erro = redis.ConnectionError("Error 111 connecting to mcp-redis:6379")
    backend = _backend(state_module, [erro], max_attempts=3, base_delay_seconds=0.1)

    assert await backend.health_check() is False
    assert backend.client.calls == 3
    assert no_sleep == [0.1, 0.2]


@pytest.mark.asyncio
async def test_save_user_data_retenta_e_recupera(state_module, no_sleep):
    erro = redis.ConnectionError("Error 111 connecting to mcp-redis:6379")
    backend = _backend(
        state_module, [erro, "OK"], max_attempts=3, base_delay_seconds=0.1
    )

    await backend.save_user_data("user-1", {"a": 1})

    assert backend.client.calls == 2
    assert no_sleep == [0.1]


@pytest.mark.asyncio
async def test_remove_user_data_esgota_e_propaga(state_module, no_sleep):
    erro = redis.ConnectionError("Error 111 connecting to mcp-redis:6379")
    backend = _backend(state_module, [erro], max_attempts=2, base_delay_seconds=0.1)

    with pytest.raises(redis.ConnectionError):
        await backend.remove_user_data("user-1")

    assert backend.client.calls == 2
    assert no_sleep == [0.1]


@pytest.mark.asyncio
async def test_delay_respeita_o_teto_configurado(state_module, no_sleep):
    """Um teto baixo limita o crescimento exponencial mesmo com muitas tentativas."""
    erro = redis.ConnectionError("Error 111 connecting to mcp-redis:6379")
    backend = _backend(
        state_module,
        [erro],
        max_attempts=6,
        base_delay_seconds=1.0,
        max_delay_seconds=3.0,
    )

    with pytest.raises(redis.ConnectionError):
        await backend.load_user_data("user-1")

    assert backend.client.calls == 6
    assert no_sleep == [1.0, 2.0, 3.0, 3.0, 3.0]


def test_orcamento_padrao_e_limitado_e_documentado(state_module):
    """O orçamento padrão de produção é finito e cabe na janela do Sentinel.

    `down-after-milliseconds` do Sentinel do `mcp-redis` é 10s (infra/superapp,
    `redis-ha-mcp/values.yaml`); o orçamento padrão precisa cobrir essa janela
    com margem, sem ser ilimitado.
    """
    backend = state_module.RedisBackend(redis_url=REDIS_URL)

    assert 2 <= backend.max_attempts <= 10
    total_wait = sum(
        min(
            backend.base_delay_seconds * (2**i),
            backend.max_delay_seconds,
        )
        for i in range(backend.max_attempts - 1)
    )
    assert 10.0 <= total_wait <= 60.0
