"""Cache do BigQuery contra um Redis de verdade (CHATR-115).

Os testes unitários usam um Redis dublê e verificam a *lógica* — quando lê,
quando grava, com que chave. Aqui o Redis é real, e o que se verifica é o que o
dublê não consegue provar: que a chave gravada é mesmo a esperada, que o TTL
chega ao servidor, que o valor sobrevive à ida e volta pelo JSON com o cliente
de verdade, e que a varredura por prefixo — a alavanca de invalidação que a
chave semântica existe para permitir — funciona no servidor.

O CI sobe `redis:7-alpine` como service; localmente basta um Redis em
`REDIS_URL`. Sem Redis alcançável, o módulo inteiro é pulado.
"""

import asyncio
import base64
import datetime
import importlib.util
import json
import os
import sys
import types
import uuid
from decimal import Decimal
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# Namespace exclusivo desta execução: os testes convivem com o que já estiver no
# Redis (estado de sessão, DLQ) e limpam apenas o que eles mesmos criaram.
NS = f"it_equipments_{uuid.uuid4().hex[:8]}"
PADRAO_DE_LIMPEZA = f"bq_cache:{NS}*"


def _redis_alcancavel() -> bool:
    try:
        import redis as redis_sync

        redis_sync.Redis.from_url(
            REDIS_URL, socket_connect_timeout=1, socket_timeout=1
        ).ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _redis_alcancavel(), reason=f"Redis não alcançável em {REDIS_URL}"
)


@pytest.fixture
def redis_cli():
    """Cliente síncrono para inspecionar o servidor, com limpeza no fim."""
    import redis as redis_sync

    cli = redis_sync.Redis.from_url(REDIS_URL, decode_responses=True)
    _limpar(cli)
    yield cli
    _limpar(cli)
    cli.close()


def _limpar(cli) -> None:
    chaves = list(cli.scan_iter(match=PADRAO_DE_LIMPEZA))
    if chaves:
        cli.delete(*chaves)


# ---------------------------------------------------------------------------
# Carga do módulo, com Redis REAL e BigQuery dublê
# ---------------------------------------------------------------------------


def _ensure_package(name: str, path: Path) -> None:
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(path)]
    sys.modules[name] = pkg


def _passthrough_interceptor(*_args, **_kwargs):
    def decorator(func):
        return func

    return decorator


def _load_module(monkeypatch, alias: str):
    _ensure_package("src", PROJECT_ROOT / "src")
    _ensure_package("src.config", PROJECT_ROOT / "src" / "config")
    _ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")

    env_module = types.SimpleNamespace(
        GCP_SERVICE_ACCOUNT_CREDENTIALS=base64.b64encode(
            json.dumps({"project_id": "proj-it"}).encode()
        ).decode(),
        GOOGLE_BIGQUERY_PAGE_SIZE=100,
        BIGQUERY_CACHE_TTL_SECONDS=3600,
        BIGQUERY_TIMEOUT_SECONDS=10.0,
        REDIS_URL=REDIS_URL,
        REDIS_CACHE_TIMEOUT_SECONDS=2.0,
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
        alias, PROJECT_ROOT / "src" / "utils" / "bigquery.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Linha(dict):
    pass


class _ClienteBQ:
    def __init__(self, linhas=None, delay=0.0, erro=None):
        self.execucoes = 0
        self._linhas = linhas if linhas is not None else [_Linha({"n": 1})]
        self._delay = delay
        self._erro = erro

    def query(self, _query, **_kwargs):
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


def _montar(monkeypatch, alias, cliente=None):
    module = _load_module(monkeypatch, alias)
    cliente = cliente if cliente is not None else _ClienteBQ()
    monkeypatch.setattr(module, "get_bigquery_client", lambda: cliente)
    return module, cliente


async def _fechar(module):
    cliente = module._async_redis_client
    if cliente is not None:
        await cliente.aclose()
        module._async_redis_client = None


# ---------------------------------------------------------------------------
# Testes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grava_no_redis_com_a_chave_semantica_esperada(monkeypatch, redis_cli):
    """A chave que chega ao servidor é a legível, não um hash."""
    module, cliente = _montar(monkeypatch, "it_bq_chave")
    try:
        await module.get_bigquery_result(
            "select * from equipamentos",
            cache_ttl_seconds=300,
            cache_namespace=NS,
            cache_key_parts={"plus8": "589R3QWR+", "cats": ["SAUDE", "EDUCACAO"]},
        )
    finally:
        await _fechar(module)

    fp = module._sql_fingerprint("select * from equipamentos")
    esperada = f"bq_cache:{NS}:cats=EDUCACAO,SAUDE:plus8=589R3QWR+:sql={fp}"
    assert redis_cli.exists(esperada) == 1
    assert cliente.execucoes == 1


@pytest.mark.asyncio
async def test_query_alterada_ocupa_chave_propria_no_servidor(monkeypatch, redis_cli):
    """Deploy que muda o SQL cria entrada nova em vez de reusar a velha.

    O dublê prova que as chaves diferem; aqui o que se verifica é o efeito no
    servidor: as duas gerações coexistem sob o mesmo prefixo, então a leitura
    nova não enxerga o formato antigo e a varredura por região continua
    pegando as duas para invalidação.
    """
    module, cliente = _montar(monkeypatch, "it_bq_versao_sql")
    partes = {"cache_namespace": NS, "cache_key_parts": {"plus8": "589R3QWR+"}}
    try:
        await module.get_bigquery_result(
            "select a from t", cache_ttl_seconds=300, **partes
        )
        await module.get_bigquery_result(
            "select a, b from t", cache_ttl_seconds=300, **partes
        )
    finally:
        await _fechar(module)

    chaves = sorted(redis_cli.scan_iter(match=f"bq_cache:{NS}:*plus8=589R3QWR+*"))
    assert len(chaves) == 2, chaves
    assert cliente.execucoes == 2


@pytest.mark.asyncio
async def test_consulta_repetida_nao_toca_no_bigquery(monkeypatch, redis_cli):
    """O critério de aceite da história, contra o Redis de verdade."""
    module, cliente = _montar(monkeypatch, "it_bq_hit")
    partes = {"cache_namespace": NS, "cache_key_parts": {"plus8": "X"}}
    try:
        primeira = await module.get_bigquery_result(
            "select 1", cache_ttl_seconds=300, **partes
        )
        segunda = await module.get_bigquery_result(
            "select 1", cache_ttl_seconds=300, **partes
        )
    finally:
        await _fechar(module)

    assert cliente.execucoes == 1
    assert primeira == segunda == [{"n": 1}]


@pytest.mark.asyncio
async def test_ttl_chega_ao_servidor_com_jitter(monkeypatch, redis_cli):
    """O TTL não é só passado ao cliente: o servidor precisa expirar a chave."""
    module, _ = _montar(monkeypatch, "it_bq_ttl")
    try:
        for i in range(8):
            await module.get_bigquery_result(
                f"select {i}",
                cache_ttl_seconds=1000,
                cache_namespace=NS,
                cache_key_parts={"i": i},
            )
    finally:
        await _fechar(module)

    ttls = [redis_cli.ttl(k) for k in redis_cli.scan_iter(match=PADRAO_DE_LIMPEZA)]
    assert len(ttls) == 8
    assert all(890 <= t <= 1000 for t in ttls), ttls
    assert len(set(ttls)) > 1, "sem jitter, todas as chaves expirariam juntas"


@pytest.mark.asyncio
async def test_invalidacao_por_regiao_com_scan_e_del(monkeypatch, redis_cli):
    """A alavanca operacional que a chave semântica existe para permitir.

    Mudança de SQL já se invalida sozinha pelo fingerprint; o que ainda
    precisa desta varredura é a correção de dado — a tabela mudou, a query
    não, e ninguém quer esperar o TTL.
    """
    module, cliente = _montar(monkeypatch, "it_bq_invalidacao")
    regiao = {"plus8": "589R3QWR+", "cats": ["SAUDE"]}
    outra = {"plus8": "589R3QXR+", "cats": ["SAUDE"]}
    try:
        for partes in (regiao, outra):
            await module.get_bigquery_result(
                "select 1",
                cache_ttl_seconds=3600,
                cache_namespace=NS,
                cache_key_parts=partes,
            )
        assert cliente.execucoes == 2

        alvo = list(redis_cli.scan_iter(match=f"bq_cache:{NS}:*plus8=589R3QWR+*"))
        assert len(alvo) == 1, alvo
        redis_cli.delete(*alvo)

        # A região apagada é reconsultada; a outra continua vindo do cache.
        await module.get_bigquery_result(
            "select 1",
            cache_ttl_seconds=3600,
            cache_namespace=NS,
            cache_key_parts=regiao,
        )
        assert cliente.execucoes == 3
        await module.get_bigquery_result(
            "select 1",
            cache_ttl_seconds=3600,
            cache_namespace=NS,
            cache_key_parts=outra,
        )
        assert cliente.execucoes == 3
    finally:
        await _fechar(module)


@pytest.mark.asyncio
async def test_ordem_das_categorias_gera_uma_unica_chave(monkeypatch, redis_cli):
    module, cliente = _montar(monkeypatch, "it_bq_ordem")
    try:
        for cats in (["SAUDE", "EDUCACAO"], ["EDUCACAO", "SAUDE"]):
            await module.get_bigquery_result(
                "select 1",
                cache_ttl_seconds=300,
                cache_namespace=NS,
                cache_key_parts={"plus8": "X", "cats": cats},
            )
    finally:
        await _fechar(module)

    assert len(list(redis_cli.scan_iter(match=PADRAO_DE_LIMPEZA))) == 1
    assert cliente.execucoes == 1


@pytest.mark.asyncio
async def test_tabela_ausente_nao_deixa_chave_no_redis(monkeypatch, redis_cli):
    module = _load_module(monkeypatch, "it_bq_notfound")

    class NotFoundFalso(Exception):
        pass

    monkeypatch.setattr(module, "NotFound", NotFoundFalso)
    monkeypatch.setattr(
        module, "get_bigquery_client", lambda: _ClienteBQ(erro=NotFoundFalso("sumiu"))
    )
    try:
        linhas = await module.get_bigquery_result(
            "select 1",
            cache_ttl_seconds=3600,
            cache_namespace=NS,
            cache_key_parts={"plus8": "X"},
        )
    finally:
        await _fechar(module)

    assert linhas == []
    assert list(redis_cli.scan_iter(match=PADRAO_DE_LIMPEZA)) == []


@pytest.mark.asyncio
async def test_ttl_zero_nao_deixa_chave_no_redis(monkeypatch, redis_cli):
    module, cliente = _montar(monkeypatch, "it_bq_bypass")
    try:
        await module.get_bigquery_result(
            "select 1",
            cache_ttl_seconds=0,
            cache_namespace=NS,
            cache_key_parts={"plus8": "X"},
        )
        await module.get_bigquery_result(
            "select 1",
            cache_ttl_seconds=0,
            cache_namespace=NS,
            cache_key_parts={"plus8": "X"},
        )
    finally:
        await _fechar(module)

    assert cliente.execucoes == 2
    assert list(redis_cli.scan_iter(match=PADRAO_DE_LIMPEZA)) == []


@pytest.mark.asyncio
async def test_single_flight_com_redis_real(monkeypatch, redis_cli):
    """10 requisições simultâneas na mesma região, uma ida ao BigQuery."""
    module, cliente = _montar(
        monkeypatch, "it_bq_single_flight", cliente=_ClienteBQ(delay=0.15)
    )
    partes = {"cache_namespace": NS, "cache_key_parts": {"plus8": "X"}}
    try:
        resultados = await asyncio.gather(
            *[
                module.get_bigquery_result("select 1", cache_ttl_seconds=300, **partes)
                for _ in range(10)
            ]
        )
    finally:
        await _fechar(module)

    assert cliente.execucoes == 1
    assert all(r == [{"n": 1}] for r in resultados)


@pytest.mark.asyncio
async def test_tipos_ricos_sobrevivem_ao_round_trip_real(monkeypatch, redis_cli):
    """Serialização real: hit e miss têm de devolver estruturas idênticas.

    É aqui que `Decimal`, `bytes` e datetime aninhado em STRUCT passam pelo
    `json.dumps`/`loads` de verdade, com o cliente de verdade — o dublê em
    memória guardaria o objeto Python e esconderia qualquer perda.
    """
    linha = _Linha(
        {
            "nome": "UPA Copacabana",
            "horario": datetime.time(8, 30),
            "contato": {"atualizado_em": datetime.datetime(2026, 4, 8, 9, 0)},
            "historico": [{"quando": datetime.date(2026, 4, 8)}],
            "valor": Decimal("10.50"),
            "blob": b"\x00\x01",
        }
    )
    module, cliente = _montar(
        monkeypatch, "it_bq_tipos", cliente=_ClienteBQ(linhas=[linha])
    )
    partes = {"cache_namespace": NS, "cache_key_parts": {"plus8": "X"}}
    try:
        no_miss = await module.get_bigquery_result(
            "select 1", cache_ttl_seconds=300, **partes
        )
        no_hit = await module.get_bigquery_result(
            "select 1", cache_ttl_seconds=300, **partes
        )
    finally:
        await _fechar(module)

    assert cliente.execucoes == 1
    assert no_miss == no_hit
    assert no_hit[0]["contato"]["atualizado_em"] == "2026-04-08T09:00:00"
    assert no_hit[0]["historico"][0]["quando"] == "2026-04-08"
    assert no_hit[0]["valor"] == 10.5
    assert no_hit[0]["blob"] == base64.b64encode(b"\x00\x01").decode()


@pytest.mark.asyncio
async def test_o_cache_nao_invade_o_espaco_de_chaves_da_sessao(monkeypatch, redis_cli):
    """Toda chave do cache vive sob `bq_cache:`.

    O estado de sessão do fluxo multi-etapas usa o `user_id` cru como chave, no
    mesmo Redis. O prefixo é o que garante que os dois não se atropelem.
    """
    module, _ = _montar(monkeypatch, "it_bq_prefixo")
    try:
        await module.get_bigquery_result(
            "select 1",
            cache_ttl_seconds=300,
            cache_namespace=NS,
            cache_key_parts={"plus8": "X"},
        )
    finally:
        await _fechar(module)

    criadas = list(redis_cli.scan_iter(match=PADRAO_DE_LIMPEZA))
    assert criadas
    assert all(k.startswith(f"{module.CACHE_KEY_PREFIX}:") for k in criadas)
