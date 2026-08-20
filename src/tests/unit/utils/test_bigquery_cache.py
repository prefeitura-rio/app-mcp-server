"""Camada de cache Redis na frente do BigQuery (CHATR-115).

Antes destes testes nenhum caminho de cache era executado pela suíte: os testes
existentes passavam `cache_ttl_seconds=0` justamente para desviar dele, então
acerto, chave, TTL, degradação e o que é ou não cacheável nunca foram exercidos.

O Redis é um dublê em memória. O que se quer verificar é a lógica de cache —
quando lê, quando grava, com que chave e com que TTL —, não o cliente do Redis.
"""

import asyncio
import base64
import datetime
import importlib.util
import json
import sys
import threading
import types
from decimal import Decimal
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[4]


# ---------------------------------------------------------------------------
# Infra dos testes
# ---------------------------------------------------------------------------


def _ensure_package(name: str, path: Path) -> types.ModuleType:
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(path)]
    sys.modules[name] = pkg
    return pkg


def _passthrough_interceptor(*_args, **_kwargs):
    def decorator(func):
        return func

    return decorator


def _load_bigquery_module(monkeypatch, module_alias: str, **env_extra):
    """Carrega `src/utils/bigquery.py` isolado, com `env` dublê.

    Cada alias produz um módulo independente, com os próprios dicionários de
    single-flight e o próprio cliente Redis — os testes não vazam entre si.
    """
    _ensure_package("src", PROJECT_ROOT / "src")
    _ensure_package("src.config", PROJECT_ROOT / "src" / "config")
    _ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")

    env_module = types.SimpleNamespace(
        GCP_SERVICE_ACCOUNT_CREDENTIALS=base64.b64encode(
            json.dumps({"project_id": "proj-cache-test"}).encode()
        ).decode(),
        GOOGLE_BIGQUERY_PAGE_SIZE=100,
        BIGQUERY_CACHE_TTL_SECONDS=3600,
        BIGQUERY_TIMEOUT_SECONDS=10.0,
        **env_extra,
    )
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
        module_alias, PROJECT_ROOT / "src" / "utils" / "bigquery.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_alias] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _RedisFalso:
    """Redis em memória, com contadores para os asserts."""

    def __init__(self):
        self.store = {}
        self.setex_calls = []
        self.get_calls = 0

    async def get(self, key):
        self.get_calls += 1
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.setex_calls.append((key, ttl, value))
        self.store[key] = value


class _RedisQuebrado:
    """Redis que responde com erro — o cache tem de degradar, não estourar."""

    async def get(self, _key):
        raise ConnectionError("redis fora do ar")

    async def setex(self, *_args):
        raise ConnectionError("redis fora do ar")


class _Linha(dict):
    """Linha do BigQuery: o código itera sobre `.items()`."""


class _ClienteBQ:
    """Client falso que conta execuções e pode simular query lenta."""

    def __init__(self, linhas=None, delay=0.0, erro=None):
        self.execucoes = 0
        self._lock = threading.Lock()
        self._linhas = linhas if linhas is not None else [_Linha({"n": 1})]
        self._delay = delay
        self._erro = erro

    def query(self, _query, **_kwargs):
        with self._lock:
            self.execucoes += 1
        outer = self

        class Job:
            def result(self, page_size=None, timeout=None):
                if outer._delay:
                    import time

                    time.sleep(outer._delay)
                if outer._erro is not None:
                    raise outer._erro
                return outer._linhas

        return Job()


def _montar(monkeypatch, alias, cliente=None, redis=None, **env_extra):
    """Carrega o módulo já com client do BigQuery e Redis substituídos."""
    module = _load_bigquery_module(monkeypatch, alias, **env_extra)
    cliente = cliente if cliente is not None else _ClienteBQ()
    redis = redis if redis is not None else _RedisFalso()
    monkeypatch.setattr(module, "get_bigquery_client", lambda: cliente)

    async def _redis_falso():
        return redis

    monkeypatch.setattr(module, "get_async_redis_client", _redis_falso)
    return module, cliente, redis


# ---------------------------------------------------------------------------
# Acerto e erro de cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_miss_executa_a_query_e_grava_no_cache(monkeypatch):
    module, cliente, redis = _montar(monkeypatch, "bq_cache_miss")

    linhas = await module.get_bigquery_result("select 1", cache_ttl_seconds=120)

    assert cliente.execucoes == 1
    assert linhas == [{"n": 1}]
    assert len(redis.setex_calls) == 1


@pytest.mark.asyncio
async def test_hit_devolve_do_cache_sem_tocar_no_bigquery(monkeypatch):
    """O ponto da história: consulta repetida não vira chamada ao BigQuery."""
    module, cliente, _redis = _montar(monkeypatch, "bq_cache_hit")

    primeira = await module.get_bigquery_result("select 1", cache_ttl_seconds=120)
    segunda = await module.get_bigquery_result("select 1", cache_ttl_seconds=120)

    assert cliente.execucoes == 1
    assert primeira == segunda


@pytest.mark.asyncio
async def test_ttl_zero_nao_le_nem_grava(monkeypatch):
    """Bypass tem de ser bypass dos dois lados, não só da leitura."""
    module, cliente, redis = _montar(monkeypatch, "bq_cache_bypass")

    await module.get_bigquery_result("select 1", cache_ttl_seconds=0)
    await module.get_bigquery_result("select 1", cache_ttl_seconds=0)

    assert cliente.execucoes == 2
    assert redis.get_calls == 0
    assert redis.setex_calls == []


@pytest.mark.asyncio
async def test_redis_fora_do_ar_degrada_para_o_bigquery(monkeypatch):
    """Cache é otimização: sem ele a resposta ainda tem de sair."""
    module, cliente, _ = _montar(
        monkeypatch, "bq_cache_redis_off", redis=_RedisQuebrado()
    )

    linhas = await module.get_bigquery_result("select 1", cache_ttl_seconds=120)

    assert linhas == [{"n": 1}]
    assert cliente.execucoes == 1


# ---------------------------------------------------------------------------
# Chave semântica
# ---------------------------------------------------------------------------


def test_chave_semantica_e_legivel_e_varrivel_por_prefixo(monkeypatch):
    """A chave precisa ser inspecionável: é o que permite invalidar por região."""
    module = _load_bigquery_module(monkeypatch, "bq_cache_chave_formato")

    chave = module._generate_cache_key(
        "select 1",
        None,
        cache_namespace="equipments",
        cache_key_parts={"plus8": "589R3QWR+", "cats": ["SAUDE", "EDUCACAO"]},
    )

    fp = module._sql_fingerprint("select 1")
    assert chave == f"bq_cache:equipments:cats=EDUCACAO,SAUDE:plus8=589R3QWR+:sql={fp}"
    assert chave.startswith(f"{module.CACHE_KEY_PREFIX}:equipments:")
    # O fingerprint entra no fim justamente para não atrapalhar a varredura
    # por prefixo, que é a alavanca de invalidação por região.
    assert len(fp) == 8


def test_ordem_das_categorias_nao_muda_a_chave(monkeypatch):
    """`IN UNNEST` não liga para ordem; a chave também não pode ligar.

    Sem isso, ["SAUDE","EDUCACAO"] e ["EDUCACAO","SAUDE"] viram duas entradas
    distintas para exatamente o mesmo resultado — o dobro de miss e de memória.
    """
    module = _load_bigquery_module(monkeypatch, "bq_cache_chave_ordem")

    def chave(cats):
        return module._generate_cache_key(
            "select 1",
            None,
            cache_namespace="equipments",
            cache_key_parts={"plus8": "X", "cats": cats},
        )

    assert chave(["SAUDE", "EDUCACAO"]) == chave(["EDUCACAO", "SAUDE"])


def test_namespaces_diferentes_nao_colidem(monkeypatch):
    module = _load_bigquery_module(monkeypatch, "bq_cache_chave_ns")

    a = module._generate_cache_key("select 1", None, "equipments", {"t": "1"})
    b = module._generate_cache_key(
        "select 1", None, "equipments_instructions", {"t": "1"}
    )

    assert a != b


def test_sem_namespace_cai_no_hash_do_sql(monkeypatch):
    """Chamador genérico continua cacheando com segurança, sem colidir."""
    module = _load_bigquery_module(monkeypatch, "bq_cache_chave_fallback")

    a = module._generate_cache_key("select 1", None)
    b = module._generate_cache_key("select  1", None)

    assert a.startswith("bq_cache:") and len(a.split(":")[1]) == 64
    assert a != b


def test_hash_de_fallback_distingue_valores_dos_parametros(monkeypatch):
    """Sem namespace, os parâmetros ainda precisam entrar na chave.

    Se não entrassem, duas consultas com o mesmo SQL e filtros diferentes
    compartilhariam a entrada — um servindo o resultado do outro.
    """
    from google.cloud import bigquery

    module = _load_bigquery_module(monkeypatch, "bq_cache_chave_fallback_params")
    sql = "select * from t where plus8 = @plus8"

    a = module._generate_cache_key(
        sql, [bigquery.ScalarQueryParameter("plus8", "STRING", "AAA")]
    )
    b = module._generate_cache_key(
        sql, [bigquery.ScalarQueryParameter("plus8", "STRING", "BBB")]
    )
    c = module._generate_cache_key(
        sql, [bigquery.ArrayQueryParameter("cats", "STRING", ["X", "Y"])]
    )

    assert len({a, b, c}) == 3


def test_parte_nula_da_chave_vira_vazio(monkeypatch):
    """`None` precisa ter representação estável, senão vira "None" literal."""
    module = _load_bigquery_module(monkeypatch, "bq_cache_chave_none")

    chave = module._generate_cache_key(
        "select 1", None, cache_namespace="equipments", cache_key_parts={"cats": None}
    )

    fp = module._sql_fingerprint("select 1")
    assert chave == f"bq_cache:equipments:cats=:sql={fp}"


def test_separador_no_valor_nao_quebra_a_estrutura_da_chave(monkeypatch):
    """Valor com `:` quebraria a varredura por prefixo se entrasse cru."""
    module = _load_bigquery_module(monkeypatch, "bq_cache_chave_separador")

    chave = module._generate_cache_key(
        "select 1",
        None,
        cache_namespace="equipments",
        cache_key_parts={"t": "a:b c"},
    )

    fp = module._sql_fingerprint("select 1")
    assert chave == f"bq_cache:equipments:t=a%3Ab%20c:sql={fp}"
    # A estrutura continua sendo `prefixo:namespace:parte=valor`: nenhum `:`
    # extra apareceu por causa do valor.
    assert chave.count(":") == 3


@pytest.mark.parametrize(
    "a,b",
    [
        ("ASSISTENCIA SOCIAL", "ASSISTENCIA_SOCIAL"),
        ("a:b", "a b"),
        ("100%", "100%25"),
    ],
    ids=["espaco-vs-underscore", "dois-pontos-vs-espaco", "porcento-literal"],
)
def test_valores_distintos_nunca_colapsam_na_mesma_chave(monkeypatch, a, b):
    """Escape injetivo: valor diferente, chave diferente, sempre.

    Trocar separador por `_` fazia esses pares colidirem — duas consultas
    semanticamente distintas dividindo uma entrada, servindo o resultado uma
    da outra por todo o TTL, sem erro e sem log.
    """
    module = _load_bigquery_module(monkeypatch, f"bq_cache_injetivo_{hash((a, b))}")

    def chave(valor):
        return module._generate_cache_key("select 1", None, "equipments", {"t": valor})

    assert chave(a) != chave(b)


def test_lista_nao_colide_com_valor_unico_que_contem_virgula(monkeypatch):
    """`["A,B"]` e `["A","B"]` são filtros diferentes e viravam a mesma chave.

    A vírgula é o separador interno das coleções; sem escapá-la, um valor que
    a contenha se disfarça de dois valores.
    """
    module = _load_bigquery_module(monkeypatch, "bq_cache_virgula")

    def chave(cats):
        return module._generate_cache_key(
            "select 1", None, "equipments", {"cats": cats}
        )

    assert chave(["A,B"]) != chave(["A", "B"])


@pytest.mark.asyncio
async def test_mudanca_no_sql_invalida_a_entrada_sozinha(monkeypatch):
    """A chave versiona o SQL: deploy que muda a query não serve dado velho.

    Antes o texto do SQL não entrava na chave semântica, e a consequência era
    aceita "desde que o deploy apague o namespace" — passo que nunca existiu
    em `k8s/` nem nos workflows. Na prática, mudar uma coluna significava
    servir o formato antigo por até uma hora.
    """
    module, cliente, _ = _montar(monkeypatch, "bq_cache_chave_versionada")

    partes = {"cache_namespace": "equipments", "cache_key_parts": {"plus8": "X"}}
    await module.get_bigquery_result("select 1", cache_ttl_seconds=120, **partes)
    await module.get_bigquery_result(
        "select 1 -- query alterada", cache_ttl_seconds=120, **partes
    )

    assert cliente.execucoes == 2


@pytest.mark.asyncio
async def test_mesmo_sql_continua_compartilhando_a_entrada(monkeypatch):
    """O outro lado: versionar o SQL não pode custar o acerto de cache normal.

    Duas chamadas iguais têm de continuar sendo uma query só — se o
    fingerprint variasse entre chamadas (por espaço em branco, por exemplo), o
    cache pararia de acertar e a mudança teria trocado um problema por outro.
    """
    module, cliente, _ = _montar(monkeypatch, "bq_cache_chave_versionada_estavel")

    partes = {"cache_namespace": "equipments", "cache_key_parts": {"plus8": "X"}}
    for _ in range(3):
        await module.get_bigquery_result("select 1", cache_ttl_seconds=120, **partes)

    assert cliente.execucoes == 1


def test_namespace_sem_partes_nao_vira_slot_global(monkeypatch):
    """Dois chamadores no mesmo namespace não podem dividir uma entrada.

    `get_category_equipments` usa namespace sem `cache_key_parts`, então a
    chave era literalmente o namespace. Um segundo chamador que copiasse esse
    padrão com outra query passaria a dividir o mesmo slot: quem gravasse
    primeiro decidiria o que o outro lê, por todo o TTL, sem erro nem log.
    """
    module = _load_bigquery_module(monkeypatch, "bq_cache_ns_sem_partes")

    a = module._generate_cache_key("select categorias", None, "equipments_categories")
    b = module._generate_cache_key("select outra coisa", None, "equipments_categories")

    assert a != b
    assert a.startswith("bq_cache:equipments_categories:")
    assert b.startswith("bq_cache:equipments_categories:")


# ---------------------------------------------------------------------------
# Resultado degradado não pode ser cacheado
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tabela_ausente_nao_e_cacheada(monkeypatch):
    """`[]` de tabela ausente não pode congelar por uma hora.

    A falha costuma ser transitória; cachear o vazio transformaria um incidente
    curto de infraestrutura em resposta errada muito depois de ele ter passado.
    """
    module = _load_bigquery_module(monkeypatch, "bq_cache_notfound")

    class NotFoundFalso(Exception):
        pass

    monkeypatch.setattr(module, "NotFound", NotFoundFalso)
    cliente = _ClienteBQ(erro=NotFoundFalso("tabela sumiu"))
    redis = _RedisFalso()
    monkeypatch.setattr(module, "get_bigquery_client", lambda: cliente)

    async def _redis_falso():
        return redis

    monkeypatch.setattr(module, "get_async_redis_client", _redis_falso)

    linhas = await module.get_bigquery_result("select 1", cache_ttl_seconds=3600)

    assert linhas == []
    assert redis.setex_calls == [], "vazio degradado não pode entrar no cache"


@pytest.mark.asyncio
async def test_tabela_ausente_e_reconsultada_na_chamada_seguinte(monkeypatch):
    """Como nada foi cacheado, a recuperação é imediata."""
    module = _load_bigquery_module(monkeypatch, "bq_cache_notfound_retry")

    class NotFoundFalso(Exception):
        pass

    monkeypatch.setattr(module, "NotFound", NotFoundFalso)
    redis = _RedisFalso()

    async def _redis_falso():
        return redis

    monkeypatch.setattr(module, "get_async_redis_client", _redis_falso)

    quebrado = _ClienteBQ(erro=NotFoundFalso("tabela sumiu"))
    monkeypatch.setattr(module, "get_bigquery_client", lambda: quebrado)
    assert await module.get_bigquery_result("select 1", cache_ttl_seconds=3600) == []

    # Tabela volta: a próxima chamada já enxerga os dados, sem esperar TTL.
    curado = _ClienteBQ(linhas=[_Linha({"n": 42})])
    monkeypatch.setattr(module, "get_bigquery_client", lambda: curado)
    assert await module.get_bigquery_result("select 1", cache_ttl_seconds=3600) == [
        {"n": 42}
    ]
    assert len(redis.setex_calls) == 1


@pytest.mark.asyncio
async def test_zero_linhas_legitimo_continua_sendo_cacheado(monkeypatch):
    """Vazio de verdade é resultado válido — e é o que mais compensa cachear."""
    module, cliente, redis = _montar(
        monkeypatch, "bq_cache_vazio_legitimo", cliente=_ClienteBQ(linhas=[])
    )

    assert await module.get_bigquery_result("select 1", cache_ttl_seconds=3600) == []
    assert len(redis.setex_calls) == 1
    assert cliente.execucoes == 1


# ---------------------------------------------------------------------------
# Single-flight e jitter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chamadas_concorrentes_na_mesma_chave_viram_uma_query(monkeypatch):
    """Estouro de cache: 10 requisições simultâneas, uma ida ao BigQuery."""
    module, cliente, _ = _montar(
        monkeypatch, "bq_cache_single_flight", cliente=_ClienteBQ(delay=0.1)
    )

    resultados = await asyncio.gather(
        *[
            module.get_bigquery_result("select 1", cache_ttl_seconds=120)
            for _ in range(10)
        ]
    )

    assert cliente.execucoes == 1
    assert all(r == [{"n": 1}] for r in resultados)


@pytest.mark.asyncio
async def test_chaves_diferentes_nao_se_bloqueiam(monkeypatch):
    """O lock é por chave: consultas distintas seguem em paralelo."""
    module, cliente, _ = _montar(
        monkeypatch, "bq_cache_single_flight_chaves", cliente=_ClienteBQ(delay=0.1)
    )

    await asyncio.gather(
        module.get_bigquery_result("select 1", cache_ttl_seconds=120),
        module.get_bigquery_result("select 2", cache_ttl_seconds=120),
    )

    assert cliente.execucoes == 2


@pytest.mark.asyncio
async def test_locks_sao_liberados_apos_uso(monkeypatch):
    """Sem limpeza, o dicionário de locks cresceria sem limite."""
    module, _cliente, _ = _montar(monkeypatch, "bq_cache_locks_limpos")

    await module.get_bigquery_result("select 1", cache_ttl_seconds=120)

    assert module._inflight_locks == {}
    assert module._inflight_refs == {}


@pytest.mark.asyncio
async def test_lock_liberado_mesmo_quando_a_query_falha(monkeypatch):
    """Exceção não pode deixar a chave travada para sempre."""
    module, _c, _r = _montar(
        monkeypatch,
        "bq_cache_lock_excecao",
        cliente=_ClienteBQ(erro=RuntimeError("boom")),
    )

    with pytest.raises(Exception, match="Failed to execute BigQuery query"):
        await module.get_bigquery_result("select 1", cache_ttl_seconds=120)

    assert module._inflight_locks == {}


@pytest.mark.asyncio
async def test_ttl_gravado_tem_jitter_para_baixo(monkeypatch):
    """Jitter desincroniza expirações; nunca serve dado mais velho que o TTL."""
    module, _cliente, redis = _montar(monkeypatch, "bq_cache_jitter")

    for i in range(30):
        await module.get_bigquery_result(f"select {i}", cache_ttl_seconds=1000)

    ttls = [ttl for _k, ttl, _v in redis.setex_calls]
    assert all(900 <= t <= 1000 for t in ttls), ttls
    assert len(set(ttls)) > 1, "TTL idêntico em toda gravação: não há jitter"


def test_jitter_preserva_ttls_muito_curtos(monkeypatch):
    module = _load_bigquery_module(monkeypatch, "bq_cache_jitter_curto")

    assert module._ttl_com_jitter(1) == 1
    assert module._ttl_com_jitter(0) == 0
    assert module._ttl_com_jitter(10) >= 1


# ---------------------------------------------------------------------------
# Consistência entre hit e miss
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hit_e_miss_devolvem_estruturas_identicas(monkeypatch):
    """A resposta não pode mudar de forma conforme o estado do cache.

    Miss devolve objetos Python; hit devolve o que voltou do JSON. Se a
    normalização não fosse recursiva, um `datetime` dentro de um STRUCT sairia
    como objeto no miss e como string no hit — mesmo tool call, tipos
    diferentes, dependendo de quem chamou antes.
    """
    linha = _Linha(
        {
            "nome": "UPA",
            "horario": datetime.time(8, 30),
            "contato": {
                "atualizado_em": datetime.datetime(2026, 4, 8, 9, 0),
                "telefone": "21999",
            },
            "historico": [{"quando": datetime.date(2026, 4, 8)}],
            "valor": Decimal("10.50"),
            "blob": b"\x00\x01",
        }
    )
    module, cliente, _ = _montar(
        monkeypatch, "bq_cache_normalizacao", cliente=_ClienteBQ(linhas=[linha])
    )

    no_miss = await module.get_bigquery_result("select 1", cache_ttl_seconds=120)
    no_hit = await module.get_bigquery_result("select 1", cache_ttl_seconds=120)

    assert cliente.execucoes == 1
    assert no_miss == no_hit
    assert no_miss[0]["contato"]["atualizado_em"] == "2026-04-08T09:00:00"
    assert no_miss[0]["historico"][0]["quando"] == "2026-04-08"
    assert no_miss[0]["valor"] == 10.5
    assert no_miss[0]["blob"] == base64.b64encode(b"\x00\x01").decode()


def test_normalizacao_e_recursiva_em_estruturas_aninhadas(monkeypatch):
    module = _load_bigquery_module(monkeypatch, "bq_cache_normalizacao_unit")

    entrada = {"a": [{"b": {"c": datetime.date(2026, 1, 2)}}]}
    assert module._normalize_bq_value(entrada) == {"a": [{"b": {"c": "2026-01-02"}}]}


# ---------------------------------------------------------------------------
# Configuração do cliente Redis
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cliente_redis_e_criado_com_timeout_de_socket(monkeypatch):
    """Sem timeout, um Redis que aceita conexão e não responde pendura o tool call.

    A leitura do cache acontece fora do `asyncio.wait_for` que protege a query,
    então o timeout do BigQuery não cobre esse caso — o socket precisa cobrir.
    """
    module = _load_bigquery_module(
        monkeypatch,
        "bq_cache_redis_timeout",
        REDIS_URL="redis://localhost:6379/0",
        REDIS_CACHE_TIMEOUT_SECONDS=2.0,
    )

    capturado = {}

    def _from_url(url, **kwargs):
        capturado["url"] = url
        capturado.update(kwargs)
        return object()

    # Substitui o `from_url` do módulo real em vez de plantar um dublê em
    # `sys.modules`: `import redis.asyncio as redis` resolve pelo atributo do
    # pacote, então o dublê seria contornado em silêncio e o teste passaria
    # sem verificar nada.
    import redis.asyncio as redis_asyncio

    monkeypatch.setattr(redis_asyncio.Redis, "from_url", staticmethod(_from_url))
    module._async_redis_client = None

    await module.get_async_redis_client()

    assert capturado["socket_connect_timeout"] == 2.0
    assert capturado["socket_timeout"] == 2.0
    assert capturado["decode_responses"] is True


@pytest.mark.asyncio
async def test_falha_ao_criar_o_cliente_redis_nao_derruba_a_query(monkeypatch):
    """Redis mal configurado degrada para o BigQuery, não estoura no import."""
    module = _load_bigquery_module(
        monkeypatch, "bq_cache_redis_init_erro", REDIS_URL="redis://localhost:6379/0"
    )

    import redis.asyncio as redis_asyncio

    def _explode(*_a, **_k):
        raise ValueError("URL malformada")

    monkeypatch.setattr(redis_asyncio.Redis, "from_url", staticmethod(_explode))
    module._async_redis_client = None
    cliente = _ClienteBQ()
    monkeypatch.setattr(module, "get_bigquery_client", lambda: cliente)

    assert await module.get_async_redis_client() is None
    assert await module.get_bigquery_result("select 1", cache_ttl_seconds=120) == [
        {"n": 1}
    ]


@pytest.mark.asyncio
async def test_valor_em_bytes_vindo_do_redis_e_decodificado(monkeypatch):
    """Defesa contra cliente sem `decode_responses`: bytes têm de virar dict."""

    class RedisBytes(_RedisFalso):
        async def get(self, key):
            valor = await super().get(key)
            return valor.encode("utf-8") if isinstance(valor, str) else valor

    module, cliente, _ = _montar(monkeypatch, "bq_cache_bytes", redis=RedisBytes())

    primeira = await module.get_bigquery_result("select 1", cache_ttl_seconds=120)
    segunda = await module.get_bigquery_result("select 1", cache_ttl_seconds=120)

    assert cliente.execucoes == 1
    assert primeira == segunda == [{"n": 1}]


# ---------------------------------------------------------------------------
# Valor corrompido no Redis
#
# Cair no `except Exception` genérico junto com falha de conexão classificava
# lixo como "Redis fora do ar" — e, como indisponibilidade faz o chamador pular
# a gravação, a chave envenenada sobrevivia até o TTL com *toda* requisição
# pagando uma query. É MISS: o Redis respondeu, ele só respondeu bobagem.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lixo",
    ["isto-nao-e-json", "", "{truncado", b"\xff\xfe nao e utf-8"],
    ids=["texto", "vazio", "json-truncado", "bytes-invalidos"],
)
async def test_valor_corrompido_e_miss_e_nao_indisponibilidade(monkeypatch, lixo):
    """A query roda e sobrescreve o lixo, em vez de conviver com ele até o TTL."""
    redis = _RedisFalso()
    module, cliente, _ = _montar(monkeypatch, "bq_cache_lixo", redis=redis)
    chave = module._generate_cache_key("select 1", None, None, None)
    redis.store[chave] = lixo

    linhas = await module.get_bigquery_result("select 1", cache_ttl_seconds=120)

    assert linhas == [{"n": 1}]
    assert cliente.execucoes == 1
    # O que separa MISS de indisponibilidade: a gravação acontece.
    assert len(redis.setex_calls) == 1
    assert json.loads(redis.store[chave]) == [{"n": 1}]


@pytest.mark.asyncio
async def test_chave_corrompida_nao_repete_query_na_proxima_chamada(monkeypatch):
    """A consequência que motivou a correção, medida em número de queries.

    Tratado como indisponibilidade, o lixo nunca era sobrescrito e cada
    requisição custava uma query nova. Duas chamadas seguidas têm de custar
    uma só.
    """
    redis = _RedisFalso()
    module, cliente, _ = _montar(monkeypatch, "bq_cache_lixo_persist", redis=redis)
    redis.store[module._generate_cache_key("select 1", None, None, None)] = "{"

    await module.get_bigquery_result("select 1", cache_ttl_seconds=120)
    await module.get_bigquery_result("select 1", cache_ttl_seconds=120)

    assert cliente.execucoes == 1


@pytest.mark.asyncio
async def test_valor_corrompido_aparece_no_span_como_corrupcao(monkeypatch):
    """Diagnóstico: lixo no cache não pode se disfarçar de erro de leitura."""
    redis = _RedisFalso()
    module, _cliente, _ = _montar(monkeypatch, "bq_cache_lixo_span", redis=redis)
    redis.store[module._generate_cache_key("select 1", None, None, None)] = "nao-json"
    tracer = _TracerFalso()
    monkeypatch.setattr(module, "get_tracer", lambda: tracer)

    await module.get_bigquery_result("select 1", cache_ttl_seconds=120)

    span = tracer.spans["bigquery.read"][0]
    assert span.attrs["cache.corrupt_value"] == "JSONDecodeError"
    # `cache.read_error` é o canal do Redis inacessível — não é o caso aqui.
    assert "cache.read_error" not in span.attrs
    assert span.attrs["cache.hit"] is False


@pytest.mark.asyncio
async def test_sem_redis_url_o_cache_fica_inerte_sem_quebrar(monkeypatch):
    """Ambiente sem Redis configurado continua servindo, direto do BigQuery."""
    module = _load_bigquery_module(monkeypatch, "bq_cache_sem_redis")
    cliente = _ClienteBQ()
    monkeypatch.setattr(module, "get_bigquery_client", lambda: cliente)

    linhas = await module.get_bigquery_result("select 1", cache_ttl_seconds=120)

    assert linhas == [{"n": 1}]
    assert cliente.execucoes == 1


# ---------------------------------------------------------------------------
# Observabilidade — critério de aceite da história
# ---------------------------------------------------------------------------


class _SpanFalso:
    def __init__(self):
        self.attrs = {}

    def set_attribute(self, key, value):
        self.attrs[key] = value

    def set_status(self, *_a):
        pass

    def record_exception(self, *_a):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _TracerFalso:
    def __init__(self):
        self.spans = {}

    def start_as_current_span(self, name):
        span = _SpanFalso()
        self.spans.setdefault(name, []).append(span)
        return span


@pytest.mark.asyncio
async def test_span_registra_acerto_e_erro_de_cache(monkeypatch):
    """Sem atributo de hit/miss não dá para medir a redução que a história pede.

    A ausência de span de query já indica economia, mas não distingue "cache
    funcionando" de "cache quebrado sem ninguém perceber".
    """
    module, _cliente, _redis = _montar(monkeypatch, "bq_cache_span")
    tracer = _TracerFalso()
    monkeypatch.setattr(module, "get_tracer", lambda: tracer)

    await module.get_bigquery_result("select 1", cache_ttl_seconds=120)
    await module.get_bigquery_result("select 1", cache_ttl_seconds=120)

    leituras = tracer.spans["bigquery.read"]
    assert leituras[0].attrs["cache.hit"] is False
    assert leituras[0].attrs["cache.written"] is True
    assert leituras[1].attrs["cache.hit"] is True
    assert leituras[0].attrs["cache.key"].startswith("bq_cache:")


@pytest.mark.asyncio
async def test_span_registra_que_o_resultado_degradado_nao_foi_cacheado(monkeypatch):
    module = _load_bigquery_module(monkeypatch, "bq_cache_span_degradado")

    class NotFoundFalso(Exception):
        pass

    monkeypatch.setattr(module, "NotFound", NotFoundFalso)
    monkeypatch.setattr(
        module, "get_bigquery_client", lambda: _ClienteBQ(erro=NotFoundFalso("x"))
    )

    async def _redis_falso():
        return _RedisFalso()

    monkeypatch.setattr(module, "get_async_redis_client", _redis_falso)
    tracer = _TracerFalso()
    monkeypatch.setattr(module, "get_tracer", lambda: tracer)

    await module.get_bigquery_result("select 1", cache_ttl_seconds=3600)

    span = tracer.spans["bigquery.read"][0]
    assert span.attrs["cache.write_skipped"] == "degraded_result"


@pytest.mark.asyncio
async def test_span_registra_falha_do_redis(monkeypatch):
    """Cache inoperante tem de aparecer no span, não só num warning de log."""
    module, _c, _r = _montar(monkeypatch, "bq_cache_span_erro", redis=_RedisQuebrado())
    tracer = _TracerFalso()
    monkeypatch.setattr(module, "get_tracer", lambda: tracer)

    await module.get_bigquery_result("select 1", cache_ttl_seconds=120)

    span = tracer.spans["bigquery.read"][0]
    assert span.attrs["cache.read_error"] == "ConnectionError"
    # A escrita nem é tentada: ver `test_falha_na_leitura_nao_tenta_gravar`.
    assert span.attrs["cache.write_skipped"] == "redis_unavailable"


@pytest.mark.asyncio
async def test_redis_que_le_mas_recusa_gravar_nao_derruba_a_consulta(monkeypatch):
    """Cenário do `noeviction` sob pressão: leitura funciona, escrita é recusada.

    Um Redis cheio com `maxmemory-policy noeviction` responde `GET` normalmente
    e rejeita `SETEX` com OOM. A consulta tem de sair mesmo assim, e a falha
    precisa aparecer no span — senão o cache fica inoperante em silêncio.
    """

    class RedisCheio(_RedisFalso):
        async def setex(self, *_args):
            raise MemoryError("OOM command not allowed when used memory > 'maxmemory'")

    module, cliente, _ = _montar(monkeypatch, "bq_cache_oom", redis=RedisCheio())
    tracer = _TracerFalso()
    monkeypatch.setattr(module, "get_tracer", lambda: tracer)

    linhas = await module.get_bigquery_result("select 1", cache_ttl_seconds=120)

    assert linhas == [{"n": 1}]
    assert cliente.execucoes == 1
    span = tracer.spans["bigquery.read"][0]
    assert span.attrs["cache.write_error"] == "MemoryError"
    assert "cache.written" not in span.attrs


@pytest.mark.asyncio
async def test_falha_na_leitura_nao_tenta_gravar(monkeypatch):
    """Redis mudo custa um timeout de socket por operação.

    Se a leitura acabou de estourar, a gravação vai estourar igual — e o
    tool call pagaria o timeout duas vezes. Medido contra um socket que aceita
    conexão e nunca responde, com `REDIS_CACHE_TIMEOUT_SECONDS=2.0`:
    6,01s antes, 2,01s depois.
    """
    tentativas = []

    class RedisMudo:
        async def get(self, _key):
            tentativas.append("get")
            raise TimeoutError("sem resposta")

        async def setex(self, *_args):
            tentativas.append("setex")
            raise TimeoutError("sem resposta")

    module, cliente, _ = _montar(
        monkeypatch, "bq_cache_sem_escrita_apos_erro", redis=RedisMudo()
    )

    linhas = await module.get_bigquery_result("select 1", cache_ttl_seconds=120)

    assert linhas == [{"n": 1}]
    assert cliente.execucoes == 1
    assert tentativas == ["get"], "não pode tentar gravar depois de a leitura falhar"
