"""Dead-letter queue do BigQuery contra um Redis de verdade (CHATR-126).

Os testes unitários usam um Redis dublê e verificam a *lógica* — quando grava,
com que chave, o que vai para poison. Aqui o Redis é real, e o que se verifica é
o que o dublê não consegue provar: que o `LTRIM` do teto corta de fato no
servidor, que o `EXPIRE` chega e é renovado, que o `SET NX` serializa duas
varreduras concorrentes, e que o ciclo do poison (mover, inspecionar,
reenfileirar) fecha contra as estruturas reais.

Vale o custo porque esta fila é a rede de segurança central da história: é ela
que separa "escrita falhou" de "registro perdido". Um dublê que divirja do
servidor no teto ou no TTL passaria despercebido justamente no caminho que só
roda quando algo já deu errado.

O CI sobe `redis:7-alpine` como service; localmente basta um Redis em
`REDIS_URL`. Sem Redis alcançável, o módulo inteiro é pulado.
"""

import base64
import importlib.util
import json
import os
import sys
import types
import uuid
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# Namespace exclusivo desta execução. A DLQ divide instância com o cache de
# queries e com o estado de sessão: os testes convivem com o que já estiver lá e
# limpam apenas o que eles mesmos criaram. Por isso também toda operação abaixo
# é chamada com `table_full_name` explícito — uma varredura sem filtro visitaria
# chaves de terceiros.
NS = uuid.uuid4().hex[:8]
TABELA = f"proj-it.ds_dlq_{NS}.tbl"
CHAVE_DLQ = f"bq_dlq:{TABELA}"
CHAVE_POISON = f"bq_dlq_poison:{TABELA}"
LOCK_DE_DRAIN = "bq_dlq_drain:lock"


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
    # O lock entra na limpeza porque a chave é global, não namespaçada: deixá-lo
    # para trás faria o próximo drain — de teste ou de gente — pular a varredura
    # até o TTL de 120s vencer.
    cli.delete(CHAVE_DLQ, CHAVE_POISON, LOCK_DE_DRAIN)


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


def _carregar(monkeypatch, alias: str, **env_extra):
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
        # Alto: o flush aqui é sempre disparado à mão, e uma thread periódica
        # acordando no meio tornaria as asserções instáveis.
        BIGQUERY_FLUSH_INTERVAL_SECONDS=3600,
        REDIS_URL=REDIS_URL,
        REDIS_DLQ_TIMEOUT_SECONDS=2.0,
    )
    for chave, valor in env_extra.items():
        setattr(env_module, chave, valor)

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
                critical=lambda *_a, **_k: None,
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
    module._flush_stop_event.set()
    return module


class _ClienteBQ:
    """Client dublê. `erros` não-vazio é a recusa por schema do BigQuery."""

    def __init__(self, erros=None, excecao=None):
        self.inserts = []
        self._erros = erros or []
        self._excecao = excecao

    def insert_rows_json(self, tabela, linhas, **_kwargs):
        self.inserts.append((tabela, list(linhas)))
        if self._excecao is not None:
            raise self._excecao
        return self._erros


def _montar(monkeypatch, alias, cliente=None, **env_extra):
    module = _carregar(monkeypatch, alias, **env_extra)
    cliente = cliente if cliente is not None else _ClienteBQ()
    monkeypatch.setattr(module, "get_bigquery_client", lambda: cliente)
    return module, cliente


# ---------------------------------------------------------------------------
# Teto e validade, medidos no servidor
# ---------------------------------------------------------------------------


def test_teto_corta_os_itens_mais_antigos_no_servidor(monkeypatch, redis_cli):
    """Sem teto, uma indisponibilidade longa do BigQuery derruba o cache junto.

    A DLQ divide instância de Redis com o cache de queries. O teto existe para
    que falha de escrita não vire degradação de leitura — e é o `LTRIM` do
    servidor que precisa de fato cortar, não a intenção de cortar.
    """
    module, _ = _montar(monkeypatch, "it_dlq_teto", **{"BIGQUERY_DLQ_MAX_ITEMS": 3})

    for i in range(5):
        module._persist_to_dlq(TABELA, [{"id": i}], "falha de teste")

    assert redis_cli.llen(CHAVE_DLQ) == 3

    # Os que sobraram são os mais recentes: o corte é pela cabeça, como no
    # `LTRIM(-max, -1)`.
    ids = [
        json.loads(bruto)["payload"][0]["id"]
        for bruto in redis_cli.lrange(CHAVE_DLQ, 0, -1)
    ]
    assert ids == [2, 3, 4]


def test_validade_chega_ao_servidor_e_e_renovada_a_cada_gravacao(
    monkeypatch, redis_cli
):
    """O TTL é o que limita a retenção do dado pessoal que vai no payload.

    Renovado a cada gravação de propósito: o relógio conta a partir da última
    escrita na chave, não por item — uma fila que ainda recebe payload não pode
    expirar no meio.
    """
    module, _ = _montar(monkeypatch, "it_dlq_ttl", **{"BIGQUERY_DLQ_TTL_SECONDS": 600})

    module._persist_to_dlq(TABELA, [{"id": 1}], "falha de teste")
    primeiro_ttl = redis_cli.ttl(CHAVE_DLQ)
    assert 0 < primeiro_ttl <= 600

    redis_cli.expire(CHAVE_DLQ, 30)
    assert redis_cli.ttl(CHAVE_DLQ) <= 30

    module._persist_to_dlq(TABELA, [{"id": 2}], "falha de teste")
    assert redis_cli.ttl(CHAVE_DLQ) > 30, "o EXPIRE não foi renovado na gravação"


def test_chave_da_dlq_e_encontrada_pela_varredura_por_prefixo(monkeypatch, redis_cli):
    """`get_dlq_depth` e o drain acham a fila por `scan_iter`, não por lista fixa.

    Se o prefixo gravado divergisse do procurado, a DLQ existiria no servidor e
    seria invisível para o health check e para o worker — dado parado que
    ninguém veria.
    """
    module, _ = _montar(monkeypatch, "it_dlq_scan")

    module._persist_to_dlq(TABELA, [{"id": 1}, {"id": 2}], "falha de teste")

    chaves = module._dlq_redis_keys(module._get_sync_redis_client())
    assert CHAVE_DLQ in chaves


# ---------------------------------------------------------------------------
# Lock de drain entre réplicas
# ---------------------------------------------------------------------------


def test_lock_serializa_duas_varreduras_concorrentes(monkeypatch, redis_cli):
    """Dois drenos simultâneos devolveriam o mesmo item duas vezes.

    A entrega é at-least-once por escolha (o item só sai da fila depois de o
    BigQuery confirmar), então duplicar é o modo de falha aceito — mas duplicar
    à toa, por falta de serialização entre réplicas, não é.
    """
    module, _ = _montar(monkeypatch, "it_dlq_lock")
    r = module._get_sync_redis_client()

    with module._dlq_drain_lock(r) as primeiro:
        assert primeiro is True
        with module._dlq_drain_lock(r) as segundo:
            assert segundo is False, "o segundo drain entrou junto com o primeiro"

    # Solto ao fim do bloco: o próximo ciclo precisa conseguir entrar.
    assert redis_cli.exists(LOCK_DE_DRAIN) == 0


def test_lock_nao_e_liberado_por_quem_nao_e_o_dono(monkeypatch, redis_cli):
    """Depois do TTL, o lock pode ser de outra réplica.

    Apagá-lo na saída sem conferir o token soltaria o dela — e os dois drenos
    passariam a rodar juntos, que é o que o lock existe para impedir.
    """
    module, _ = _montar(monkeypatch, "it_dlq_lock_dono")
    r = module._get_sync_redis_client()

    with module._dlq_drain_lock(r) as adquirido:
        assert adquirido is True
        # Simula o TTL vencendo e outra réplica assumindo o lock.
        redis_cli.set(LOCK_DE_DRAIN, "token-de-outra-replica", ex=120)

    assert redis_cli.get(LOCK_DE_DRAIN) == "token-de-outra-replica"


# ---------------------------------------------------------------------------
# Reprocessamento e ciclo do poison
# ---------------------------------------------------------------------------


def test_replay_devolve_ao_bigquery_e_esvazia_a_fila_no_servidor(
    monkeypatch, redis_cli
):
    """A ordem é insert e depois LPOP, nunca o contrário.

    Uma queda entre a confirmação e a remoção reprocessa o item, ou seja,
    duplica. Para log, feedback e alerta, registro duplicado é incômodo de
    análise; registro perdido é o defeito que a história existe para eliminar.
    """
    module, cliente = _montar(monkeypatch, "it_dlq_replay")

    module._persist_to_dlq(TABELA, [{"id": 1}, {"id": 2}], "falha de teste")
    assert redis_cli.llen(CHAVE_DLQ) == 1

    resumo = module._replay_dlq_redis(10, table_full_name=TABELA)

    assert resumo["itens"] == 1
    assert resumo["linhas"] == 2
    assert cliente.inserts and cliente.inserts[0][0] == TABELA
    assert redis_cli.llen(CHAVE_DLQ) == 0


def test_dry_run_reporta_sem_consumir_a_fila(monkeypatch, redis_cli):
    """Conferir antes de mexer não pode alterar o que se está conferindo."""
    module, cliente = _montar(monkeypatch, "it_dlq_dryrun")

    module._persist_to_dlq(TABELA, [{"id": 1}], "falha de teste")

    resumo = module._replay_dlq_redis(10, table_full_name=TABELA, dry_run=True)

    assert resumo["itens"] == 1
    assert cliente.inserts == [], "o dry-run escreveu no BigQuery"
    assert redis_cli.llen(CHAVE_DLQ) == 1


def test_ciclo_do_poison_move_inspeciona_e_reenfileira(monkeypatch, redis_cli):
    """O ciclo inteiro contra as estruturas reais.

    Um payload que o BigQuery recusa por schema não melhora com o tempo: sem
    sair da frente, ele bloquearia para sempre tudo o que chegou depois. E sem
    caminho de volta, sair da frente seria só trocar "perdido" por "parado numa
    lista que ninguém lê" — daí a inspeção e o reenfileiramento.
    """
    module, _ = _montar(
        monkeypatch,
        "it_dlq_poison",
        cliente=_ClienteBQ(
            erros=[{"index": 0, "errors": [{"message": "no such field: cep"}]}]
        ),
    )

    module._persist_to_dlq(TABELA, [{"id": 1, "cep": "20000-000"}], "falha de teste")

    # 1. O drain recusa em definitivo e move para a chave de poison.
    resumo = module._replay_dlq_redis(10, table_full_name=TABELA)
    assert resumo["poison"] == 1
    assert redis_cli.llen(CHAVE_DLQ) == 0
    assert redis_cli.llen(CHAVE_POISON) == 1
    assert 0 < redis_cli.ttl(CHAVE_POISON) <= 604800

    # 2. A inspeção lê sem consumir e sem expor o conteúdo do payload.
    inspecao = module.inspecionar_poison(table_full_name=TABELA)
    assert inspecao["total"] == 1
    item = inspecao["itens"][0]
    assert item["campos"] == ["cep", "id"]
    assert "no such field: cep" in item["erro"]
    assert "payload" not in item, "o conteúdo do payload vazou na listagem padrão"
    assert redis_cli.llen(CHAVE_POISON) == 1

    # 3. Corrigida a causa, o item volta para a fila normal.
    devolvido = module.reenfileirar_poison(table_full_name=TABELA)
    assert devolvido["itens"] == 1
    assert redis_cli.llen(CHAVE_POISON) == 0
    assert redis_cli.llen(CHAVE_DLQ) == 1


def test_teto_do_poison_corta_no_servidor_e_a_perda_vai_ao_log(monkeypatch, redis_cli):
    """O poison tem teto, logo tem descarte — e descarte é perda definitiva.

    O dublê prova que o código pede o `LTRIM`; só o servidor prova que ele corta
    e quanto. É a diferença entre um item que o operador vai procurar no poison
    e um item que já não existe em lugar nenhum — daí a exigência do log.
    """
    module, _ = _montar(
        monkeypatch,
        "it_poison_teto",
        cliente=_ClienteBQ(erros=[{"index": 0, "errors": [{"message": "schema"}]}]),
        **{"BIGQUERY_DLQ_MAX_ITEMS": 2},
    )
    criticos = []
    monkeypatch.setattr(
        module.logger, "critical", lambda msg, *_a, **_k: criticos.append(msg)
    )

    # Um por vez: a DLQ divide o mesmo teto, e enfileirar os três de uma vez
    # cortaria lá antes de chegar aqui.
    for i in range(3):
        module._persist_to_dlq(TABELA, [{"id": i}], "falha de teste")
        module._replay_dlq_redis(10, table_full_name=TABELA)

    assert redis_cli.llen(CHAVE_POISON) == 2
    ids = [
        json.loads(bruto)["payload"][0]["id"]
        for bruto in redis_cli.lrange(CHAVE_POISON, 0, -1)
    ]
    assert ids == [1, 2], "o corte não foi pela cabeça da lista"
    assert any("DESCARTADOS" in msg for msg in criticos), (
        "o item cortado do poison sumiu do servidor sem log crítico"
    )
    assert module.get_bigquery_write_metrics()["dlq_items_dropped"] == 1


def test_reenfileirar_para_dlq_no_teto_reporta_o_que_foi_cortado(
    monkeypatch, redis_cli
):
    """A DLQ segue recebendo enquanto o operador devolve o poison.

    Devolver para uma fila que já está no teto expulsa a cabeça dela. O comando
    precisa reportar isso: o saldo da operação pode ser negativo, e no servidor
    o item cortado já não existe para ser recuperado.
    """
    module, _ = _montar(monkeypatch, "it_requeue_teto", **{"BIGQUERY_DLQ_MAX_ITEMS": 2})
    criticos = []
    monkeypatch.setattr(
        module.logger, "critical", lambda msg, *_a, **_k: criticos.append(msg)
    )

    # DLQ no teto, e um item esperando no poison para voltar.
    for i in range(2):
        module._persist_to_dlq(TABELA, [{"id": i}], "falha de teste")
    redis_cli.rpush(
        CHAVE_POISON,
        json.dumps(
            {
                "table_full_name": TABELA,
                "failed_at": "2026-08-20T10:00:00.000000",
                "error": "no such field: cep",
                "payload": [{"id": 99}],
            }
        ),
    )

    resumo = module.reenfileirar_poison(table_full_name=TABELA)

    assert resumo["itens"] == 1
    assert resumo["descartados"] == 1, "a perda não chegou ao resumo do comando"
    assert redis_cli.llen(CHAVE_DLQ) == 2
    ids = [
        json.loads(bruto)["payload"][0]["id"]
        for bruto in redis_cli.lrange(CHAVE_DLQ, 0, -1)
    ]
    assert ids == [1, 99], "o mais antigo da DLQ devia ter sido o cortado"
    assert any("DESCARTADOS" in msg for msg in criticos)


def test_inspecao_com_payload_e_opt_in(monkeypatch, redis_cli):
    """O payload carrega telefone, endereço e coordenada.

    A saída do CLI vai para o terminal do operador e, com frequência, para o
    scrollback ou para o log de um job. Sair por padrão espalharia o dado para
    fora dos lugares onde ele é controlado.
    """
    module, _ = _montar(
        monkeypatch,
        "it_dlq_poison_payload",
        cliente=_ClienteBQ(erros=[{"index": 0, "errors": [{"message": "schema"}]}]),
    )

    module._persist_to_dlq(TABELA, [{"user_id": "21999999999"}], "falha de teste")
    module._replay_dlq_redis(10, table_full_name=TABELA)

    com_payload = module.inspecionar_poison(
        table_full_name=TABELA, incluir_payload=True
    )
    assert com_payload["itens"][0]["payload"] == [{"user_id": "21999999999"}]


def test_descartar_poison_apaga_a_chave_no_servidor(monkeypatch, redis_cli):
    """Descarte é irreversível e explícito — o TTL faria o mesmo em silêncio."""
    module, _ = _montar(
        monkeypatch,
        "it_dlq_poison_purge",
        cliente=_ClienteBQ(erros=[{"index": 0, "errors": [{"message": "schema"}]}]),
    )

    module._persist_to_dlq(TABELA, [{"id": 1}], "falha de teste")
    module._replay_dlq_redis(10, table_full_name=TABELA)
    assert redis_cli.llen(CHAVE_POISON) == 1

    resumo = module.descartar_poison(table_full_name=TABELA)

    assert resumo["itens"] == 1
    assert redis_cli.exists(CHAVE_POISON) == 0
