"""Teto de latência da leitura do BigQuery sob concorrência (CHATR-125).

O critério de aceite da subtarefa é que "uma consulta lenta ao BigQuery não
trava mais o event loop nem o tool call indefinidamente". O `asyncio.wait_for`
sozinho não entregava isso: ele fica *dentro* do lock de single-flight, então
limitava a query e não a fila de quem esperava por ela. Com uma query lenta na
frente, a enésima chamada da mesma chave esperava n × timeout — e isso
acontecia justamente no cenário que o cache existe para atender, todo mundo
pedindo a mesma região ao mesmo tempo.

Os testes deste arquivo cobrem o caminho que faltava: concorrência **com a
query falhando**. Os testes de single-flight que já existiam usam só o caminho
feliz, em que a primeira query responde e as demais são servidas pelo cache —
nele o defeito é invisível.

O modelo é de orçamento único: `timeout_seconds` é o prazo da chamada inteira
(cache + fila + query), não de cada etapa.
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

# Prazo curto para os testes rodarem rápido, e query bem mais lenta que ele:
# assim o desfecho é sempre o estouro, que é o que se quer medir.
ORCAMENTO = 0.5
QUERY_LENTA = 3.0
CONCORRENTES = 6


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
            json.dumps({"project_id": "proj-deadline-test"}).encode()
        ).decode(),
        "GOOGLE_BIGQUERY_PAGE_SIZE": 100,
        "BIGQUERY_TIMEOUT_SECONDS": 10.0,
        "BIGQUERY_CACHE_TTL_SECONDS": 3600,
        # Pool folgado: o objetivo é medir a fila do single-flight, não a fila
        # por vaga de thread.
        "BIGQUERY_READ_MAX_WORKERS": CONCORRENTES + 2,
    }
    valores.update(env_extra)
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


class _ClienteContado:
    """Client falso que conta execuções e dorme `delay` dentro da thread."""

    def __init__(self, delay: float, linhas=None):
        self.delay = delay
        self.execucoes = 0
        self._lock = threading.Lock()
        self._linhas = linhas if linhas is not None else []

    def query(self, _query, **_kwargs):
        with self._lock:
            self.execucoes += 1
        outer = self

        class Job:
            def result(self, page_size=None, timeout=None):
                time.sleep(outer.delay)
                return outer._linhas

            def cancel(self):
                return None

        return Job()


def _montar(monkeypatch, alias, delay=QUERY_LENTA, redis=None, **env_extra):
    module = _load_bigquery_module(monkeypatch, alias, **env_extra)
    cliente = _ClienteContado(delay)
    monkeypatch.setattr(module, "get_bigquery_client", lambda: cliente)
    redis = redis if redis is not None else _RedisFalso()

    async def _redis_falso():
        return redis

    monkeypatch.setattr(module, "get_async_redis_client", _redis_falso)
    monkeypatch.setattr(module, "_shutdown_read_executor", lambda: None, raising=False)
    return module, cliente, redis


async def _medir(module, **kwargs):
    """Roda uma leitura e devolve (duração, exceção ou None)."""
    inicio = time.monotonic()
    try:
        await module.get_bigquery_result(**kwargs)
        erro = None
    except BaseException as e:  # noqa: BLE001 - o teste inspeciona o tipo
        erro = e
    return time.monotonic() - inicio, erro


# ---------------------------------------------------------------------------
# O teto propriamente dito
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concorrentes_na_mesma_chave_respeitam_o_teto(monkeypatch):
    """A regressão que motivou o teto: n chamadas, n × timeout de espera.

    Antes, com a query sempre estourando, cada um que esperava na fila começava
    o próprio prazo *depois* do anterior desistir — medido em produção
    simulada: 6 chamadas com prazo de 0,3s, a última levando 1,82s. Com o
    padrão de 10s isso vira quase um minuto para o último da fila.

    O limite do assert é generoso de propósito (3× o orçamento): o que importa
    é separar "teto constante" de "cresce com a concorrência", não cronometrar
    a máquina do CI.
    """
    module, _cliente, _redis = _montar(monkeypatch, "bq_deadline_teto")

    partes = {"cache_namespace": "eq", "cache_key_parts": {"plus8": "MESMA"}}
    resultados = await asyncio.gather(
        *[
            _medir(
                module,
                query="select 1",
                cache_ttl_seconds=120,
                timeout_seconds=ORCAMENTO,
                **partes,
            )
            for _ in range(CONCORRENTES)
        ]
    )

    duracoes = [d for d, _ in resultados]
    assert max(duracoes) < ORCAMENTO * 3, duracoes
    # E o teto não é acidente de uma chamada só: nenhuma delas escapou.
    assert all(isinstance(erro, TimeoutError) for _d, erro in resultados)


@pytest.mark.asyncio
async def test_teto_nao_cresce_ao_dobrar_a_concorrencia(monkeypatch):
    """O sintoma era proporcionalidade: 2× chamadas, 2× espera do último.

    Comparar duas concorrências no mesmo teste é o que distingue "está lento"
    de "está lento porque tem fila" — só o segundo é o defeito.
    """

    async def pior_caso(alias, n):
        module, _c, _r = _montar(monkeypatch, alias)
        partes = {"cache_namespace": "eq", "cache_key_parts": {"plus8": "MESMA"}}
        resultados = await asyncio.gather(
            *[
                _medir(
                    module,
                    query="select 1",
                    cache_ttl_seconds=120,
                    timeout_seconds=ORCAMENTO,
                    **partes,
                )
                for _ in range(n)
            ]
        )
        return max(d for d, _ in resultados)

    poucos = await pior_caso("bq_deadline_escala_2", 2)
    muitos = await pior_caso("bq_deadline_escala_8", 8)

    # Antes, `muitos` era ~4× `poucos`. Agora os dois batem no mesmo teto.
    assert muitos < poucos + ORCAMENTO, (poucos, muitos)


@pytest.mark.asyncio
async def test_quem_espera_na_fila_nao_ganha_orcamento_novo(monkeypatch):
    """O prazo é da chamada, não de cada etapa dela.

    Se a espera no lock não consumisse orçamento, o segundo da fila esperaria o
    primeiro desistir e só então começaria a contar os seus 0,5s — que é
    exatamente como o total virava n × timeout.
    """
    module, cliente, _redis = _montar(monkeypatch, "bq_deadline_orcamento_unico")
    partes = {"cache_namespace": "eq", "cache_key_parts": {"plus8": "MESMA"}}

    async def leitura():
        return await _medir(
            module,
            query="select 1",
            cache_ttl_seconds=120,
            timeout_seconds=ORCAMENTO,
            **partes,
        )

    primeiro = asyncio.create_task(leitura())
    await asyncio.sleep(0.05)  # garante a ordem de chegada na fila
    segundo = asyncio.create_task(leitura())

    (d1, e1), (d2, e2) = await asyncio.gather(primeiro, segundo)

    assert isinstance(e1, TimeoutError) and isinstance(e2, TimeoutError)
    # Antes: 0,5s esperando o primeiro desistir + 0,5s de prazo novo = ~2×.
    # Agora a espera sai do mesmo bolso, e o total fica no próprio orçamento.
    assert d2 < ORCAMENTO * 1.5, (d1, d2)
    assert cliente.execucoes <= 2


@pytest.mark.asyncio
async def test_orcamento_esgotado_na_fila_nao_dispara_query(monkeypatch):
    """Query cujo resultado ninguém vai esperar é só custo no BigQuery."""
    module, cliente, _redis = _montar(monkeypatch, "bq_deadline_sem_query_inutil")
    partes = {"cache_namespace": "eq", "cache_key_parts": {"plus8": "MESMA"}}

    await asyncio.gather(
        *[
            _medir(
                module,
                query="select 1",
                cache_ttl_seconds=120,
                timeout_seconds=ORCAMENTO,
                **partes,
            )
            for _ in range(CONCORRENTES)
        ]
    )

    # Só quem pegou o lock consultou; os outros nem chegaram a submeter.
    assert cliente.execucoes == 1


@pytest.mark.asyncio
async def test_mensagem_do_estouro_cita_o_orcamento_e_nao_o_residuo(monkeypatch):
    """Quem lê o log quer saber o limite configurado, não quanto sobrava.

    Sem isso a mensagem sai com o resíduo em ponto flutuante
    ("timed out after 0.19998779099842068s"), que não diz nada a quem está
    investigando.
    """
    module, _cliente, _redis = _montar(monkeypatch, "bq_deadline_mensagem")

    _d, erro = await _medir(
        module, query="select 1", cache_ttl_seconds=0, timeout_seconds=ORCAMENTO
    )

    assert isinstance(erro, TimeoutError)
    assert f"timed out after {ORCAMENTO}s" in str(erro)


# ---------------------------------------------------------------------------
# O teto não pode ter custado o que já funcionava
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_caminho_feliz_continua_coalescendo(monkeypatch):
    """Com a query respondendo a tempo, o single-flight segue valendo por 1."""
    module, cliente, _redis = _montar(monkeypatch, "bq_deadline_feliz", delay=0.05)
    partes = {"cache_namespace": "eq", "cache_key_parts": {"plus8": "MESMA"}}

    await asyncio.gather(
        *[
            module.get_bigquery_result(
                "select 1", cache_ttl_seconds=120, timeout_seconds=5, **partes
            )
            for _ in range(CONCORRENTES)
        ]
    )

    assert cliente.execucoes == 1


@pytest.mark.asyncio
async def test_chaves_diferentes_nao_disputam_o_mesmo_prazo(monkeypatch):
    """O lock é por chave: região diferente não entra na fila da outra."""
    module, cliente, _redis = _montar(
        monkeypatch, "bq_deadline_chaves_livres", delay=0.05
    )

    duracoes = await asyncio.gather(
        *[
            _medir(
                module,
                query="select 1",
                cache_ttl_seconds=120,
                timeout_seconds=5,
                cache_namespace="eq",
                cache_key_parts={"plus8": f"REGIAO-{i}"},
            )
            for i in range(CONCORRENTES)
        ]
    )

    assert cliente.execucoes == CONCORRENTES
    assert all(erro is None for _d, erro in duracoes)


@pytest.mark.asyncio
async def test_desistir_na_fila_nao_vaza_lock(monkeypatch):
    """Caminho novo: quem estoura *antes* de adquirir também precisa limpar.

    Os testes de limpeza que existiam cobrem quem chegou a segurar o lock. Sem
    este, um vazamento no ramo de desistência só apareceria como consumo de
    memória crescendo devagar em produção.
    """
    module, _cliente, _redis = _montar(monkeypatch, "bq_deadline_sem_vazamento")
    partes = {"cache_namespace": "eq", "cache_key_parts": {"plus8": "MESMA"}}

    await asyncio.gather(
        *[
            _medir(
                module,
                query="select 1",
                cache_ttl_seconds=120,
                timeout_seconds=ORCAMENTO,
                **partes,
            )
            for _ in range(CONCORRENTES)
        ]
    )

    assert module._inflight_locks == {}
    assert module._inflight_refs == {}


@pytest.mark.asyncio
async def test_desistir_no_instante_da_entrega_nao_trava_a_chave(monkeypatch):
    """A borda: o dono libera o lock no mesmo instante em que o prazo estoura.

    É a única janela em que "desistir" e "adquirir" podem acontecer na mesma
    iteração do event loop. Se o `acquire()` conseguir o lock e o chamador
    ainda assim sair pelo caminho do estouro, ninguém chama o `release()` — e
    a chave (na prática um `plus8` específico) fica travada até o processo
    reiniciar, transformando um timeout isolado em pane permanente naquela
    região.

    O teste varre um punhado de deslocamentos em torno do prazo para cair dos
    dois lados da janela, e cobra sempre a mesma invariante: acabou a chamada,
    o lock está livre.
    """
    module = _load_bigquery_module(monkeypatch, "bq_deadline_corrida_entrega")
    loop = asyncio.get_running_loop()
    prazo = 0.02

    for i, desvio in enumerate((-0.002, -0.0005, 0.0, 0.0005, 0.002)):
        chave = f"bq_cache:corrida:{i}"
        lock = asyncio.Lock()
        module._inflight_locks[chave] = lock
        module._inflight_refs[chave] = 1  # o dono, simulado à mão
        await lock.acquire()

        deadline = loop.time() + prazo

        async def esperador():
            try:
                async with module._single_flight(chave, deadline=deadline):
                    return "adquiriu"
            except module.BigQueryTimeoutError:
                return "estourou"

        loop.call_at(deadline + desvio, lock.release)
        tarefa = asyncio.create_task(esperador())

        assert await tarefa in ("adquiriu", "estourou")

        # Deixa o `release` agendado disparar antes de olhar para o lock:
        # com desvio positivo ele ainda não rodou quando a tarefa termina.
        await asyncio.sleep(max(0.0, deadline + desvio - loop.time()) + 0.005)

        # O dono sai de cena; o que sobra é só o estado do lock.
        module._inflight_refs[chave] = module._inflight_refs.get(chave, 1) - 1
        if module._inflight_refs[chave] <= 0:
            module._inflight_refs.pop(chave, None)
            module._inflight_locks.pop(chave, None)

        assert not lock.locked(), f"lock ficou preso com desvio={desvio}"

    assert module._inflight_locks == {}
    assert module._inflight_refs == {}


@pytest.mark.asyncio
async def test_event_loop_segue_livre_com_a_fila_cheia(monkeypatch):
    """A fila é de corrotinas esperando, não de threads travando o loop."""
    module, _cliente, _redis = _montar(monkeypatch, "bq_deadline_loop_livre")
    partes = {"cache_namespace": "eq", "cache_key_parts": {"plus8": "MESMA"}}

    batidas = []

    async def heartbeat():
        while True:
            batidas.append(time.monotonic())
            await asyncio.sleep(0.02)

    hb = asyncio.create_task(heartbeat())
    await asyncio.gather(
        *[
            _medir(
                module,
                query="select 1",
                cache_ttl_seconds=120,
                timeout_seconds=ORCAMENTO,
                **partes,
            )
            for _ in range(CONCORRENTES)
        ]
    )
    hb.cancel()

    intervalos = [b - a for a, b in zip(batidas, batidas[1:])]
    assert max(intervalos) < 0.2, max(intervalos)


# ---------------------------------------------------------------------------
# A gravação do cache também sai do orçamento
#
# Ela acontece depois de a query já ter respondido, então ficava fora do prazo
# que `get_bigquery_result` promete: com o Redis mudo, o tool call somava o
# timeout do BigQuery ao timeout de socket do Redis. Medido antes da correção:
# +4,0s sobre um orçamento de 5s.
# ---------------------------------------------------------------------------


class _RedisQuePendura(_RedisFalso):
    """Lê rápido (miss) e pendura na gravação, como um Redis particionado."""

    def __init__(self, atraso: float):
        super().__init__()
        self.atraso = atraso
        self.setex_concluidos = 0

    async def setex(self, key, ttl, value):
        await asyncio.sleep(self.atraso)
        self.setex_concluidos += 1
        await super().setex(key, ttl, value)


@pytest.mark.asyncio
async def test_gravacao_lenta_nao_estoura_o_teto_da_chamada(monkeypatch):
    """O teto da chamada continua sendo o timeout configurado."""
    redis = _RedisQuePendura(atraso=QUERY_LENTA)
    module, _cliente, _ = _montar(
        monkeypatch, "bq_deadline_write", delay=0.05, redis=redis
    )

    duracao, erro = await _medir(
        module, query="select 1", cache_ttl_seconds=120, timeout_seconds=ORCAMENTO
    )

    assert erro is None, erro
    assert duracao < ORCAMENTO * 1.5, duracao


@pytest.mark.asyncio
async def test_gravacao_abandonada_ainda_devolve_as_linhas(monkeypatch):
    """Desistir do cache não pode custar o resultado — a query já respondeu.

    É o ponto em que este prazo difere do da query: lá o estouro é falha, aqui
    é só um miss a mais na próxima requisição.
    """
    redis = _RedisQuePendura(atraso=QUERY_LENTA)
    module, _cliente, _ = _montar(
        monkeypatch, "bq_deadline_write_rows", delay=0.05, redis=redis
    )
    monkeypatch.setattr(
        module,
        "_execute_bigquery_query",
        lambda *a, **k: module._QueryOutcome([{"n": 7}], True),
    )

    linhas = await module.get_bigquery_result(
        query="select 1", cache_ttl_seconds=120, timeout_seconds=ORCAMENTO
    )

    assert linhas == [{"n": 7}]
    assert redis.setex_concluidos == 0


@pytest.mark.asyncio
async def test_gravacao_dentro_do_prazo_continua_acontecendo(monkeypatch):
    """O limite não pode virar um cache que nunca grava."""
    redis = _RedisQuePendura(atraso=0.01)
    module, cliente, _ = _montar(
        monkeypatch, "bq_deadline_write_ok", delay=0.05, redis=redis
    )

    await module.get_bigquery_result(
        query="select 1", cache_ttl_seconds=120, timeout_seconds=ORCAMENTO
    )

    assert redis.setex_concluidos == 1
    assert cliente.execucoes == 1


@pytest.mark.asyncio
async def test_orcamento_ja_esgotado_nem_tenta_gravar(monkeypatch):
    """Sem prazo restante a gravação é pulada, e o span diz por quê."""
    redis = _RedisQuePendura(atraso=QUERY_LENTA)
    module, _cliente, _ = _montar(
        monkeypatch, "bq_deadline_write_skip", delay=0.05, redis=redis
    )

    class _Span:
        def __init__(self):
            self.attrs = {}

        def set_attribute(self, k, v):
            self.attrs[k] = v

        def set_status(self, *_a):
            pass

        def record_exception(self, *_a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    span = _Span()
    monkeypatch.setattr(
        module,
        "get_tracer",
        lambda: types.SimpleNamespace(start_as_current_span=lambda _n: span),
    )
    await module._cache_write("chave", [{"n": 1}], 120, span, restante=0)

    assert span.attrs["cache.write_skipped"] == "budget_exhausted"
    assert redis.setex_concluidos == 0
