"""Agrupamento de escritas e reprocessamento da DLQ (CHATR-118 / CHATR-126).

O que estes testes prendem, em ordem de gravidade:

1. O buffer é drenado no encerramento por sinal. Sem isso, `atexit` era a única
   rede — e ela não fecha em SIGTERM, porque a uvicorn re-levanta o sinal com o
   handler default e o processo morre antes. O efeito era perder até um lote
   inteiro por rollout, em silêncio.
2. A DLQ tem volta. Persistir o payload sem caminho de retorno só troca "dado
   perdido" por "dado parado numa lista que ninguém lê".
3. Um payload que o BigQuery nunca vai aceitar sai da frente. Sem isso, um
   registro malformado na cabeça da fila bloquearia todos os que vieram depois.
"""

import base64
import importlib.util
import json
import signal
import sys
import threading
import types
from pathlib import Path

import pytest

from google.api_core.exceptions import BadRequest, ServiceUnavailable


PROJECT_ROOT = Path(__file__).resolve().parents[4]


def _ensure_package(name: str, path: Path) -> None:
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(path)]
    sys.modules[name] = pkg


def _passthrough_interceptor(*_args, **_kwargs):
    def decorator(func):
        return func

    return decorator


# Cada teste carrega o módulo sob um alias próprio, e o módulo sobrevive ao
# teste. Sem limpar o buffer no teardown, as linhas que sobraram nele chegam ao
# `atexit` registrado no import — já sem os monkeypatches — e o flush final
# tenta o BigQuery de verdade, caindo na DLQ em arquivo dentro do repositório.
_MODULOS_CARREGADOS: list = []


@pytest.fixture(autouse=True)
def _esvaziar_buffers():
    yield
    for module in _MODULOS_CARREGADOS:
        with module._batch_buffer_lock:
            module._batch_buffer.clear()
    _MODULOS_CARREGADOS.clear()


def _carregar_bigquery(monkeypatch, alias: str, **env_extra):
    """Carrega `src/utils/bigquery.py` isolado, com um `env` sintético."""
    _ensure_package("src", PROJECT_ROOT / "src")
    _ensure_package("src.config", PROJECT_ROOT / "src" / "config")
    _ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")

    env_module = types.SimpleNamespace(
        GCP_SERVICE_ACCOUNT_CREDENTIALS=base64.b64encode(
            json.dumps({"project_id": "proj-batch-test"}).encode()
        ).decode(),
        GOOGLE_BIGQUERY_PAGE_SIZE=100,
        BIGQUERY_CACHE_TTL_SECONDS=3600,
        BIGQUERY_TIMEOUT_SECONDS=10.0,
        # Intervalo alto: nestes testes o flush é sempre disparado à mão, e uma
        # thread periódica acordando no meio tornaria as asserções instáveis.
        BIGQUERY_FLUSH_INTERVAL_SECONDS=3600,
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
    # A thread periódica é irrelevante aqui e só adiciona ruído entre testes.
    module._flush_stop_event.set()
    _MODULOS_CARREGADOS.append(module)
    return module


def _espionar_bigquery(monkeypatch, module, erro=None):
    """Substitui o client do BigQuery por um que registra cada insert."""
    chamadas = []

    def _insert_rows_json(tabela, linhas, **_kwargs):
        chamadas.append((tabela, list(linhas)))
        if erro is not None:
            raise erro
        return []

    monkeypatch.setattr(
        module,
        "get_bigquery_client",
        lambda: types.SimpleNamespace(insert_rows_json=_insert_rows_json),
    )
    return chamadas


# ---------------------------------------------------------------------------
# Agrupamento
# ---------------------------------------------------------------------------


def test_linhas_abaixo_do_limiar_nao_vao_ao_bigquery(monkeypatch):
    """O ganho do batching é exatamente este: não chamar o BigQuery por linha."""
    module = _carregar_bigquery(monkeypatch, "bq_batch_limiar", BIGQUERY_BATCH_SIZE=5)
    chamadas = _espionar_bigquery(monkeypatch, module)

    for i in range(4):
        module.enqueue_bigquery_row("proj.ds.tbl", {"i": i})

    assert chamadas == []
    assert module.get_bigquery_write_metrics()["rows_buffered"] == 4


def test_ao_atingir_o_limiar_o_lote_vai_numa_chamada_so(monkeypatch):
    """Cinco linhas, uma chamada — é o critério de aceite do CHATR-118."""
    module = _carregar_bigquery(monkeypatch, "bq_batch_lote", BIGQUERY_BATCH_SIZE=5)
    chamadas = _espionar_bigquery(monkeypatch, module)

    for i in range(5):
        module.enqueue_bigquery_row("proj.ds.tbl", {"i": i})

    assert len(chamadas) == 1
    assert chamadas[0][0] == "proj.ds.tbl"
    assert len(chamadas[0][1]) == 5

    metricas = module.get_bigquery_write_metrics()
    assert metricas["rows_enqueued"] == 5
    assert metricas["rows_written"] == 5
    assert metricas["insert_calls"] == 1
    assert metricas["rows_buffered"] == 0


def test_flush_manual_drena_o_que_sobrou(monkeypatch):
    """Volume baixo não pode deixar linha presa em memória para sempre."""
    module = _carregar_bigquery(monkeypatch, "bq_batch_flush", BIGQUERY_BATCH_SIZE=100)
    chamadas = _espionar_bigquery(monkeypatch, module)

    module.enqueue_bigquery_row("proj.ds.a", {"i": 1})
    module.enqueue_bigquery_row("proj.ds.b", {"i": 2})
    assert chamadas == []

    module.flush_bigquery_batch_buffer()

    assert sorted(tabela for tabela, _ in chamadas) == ["proj.ds.a", "proj.ds.b"]
    assert module.get_bigquery_write_metrics()["rows_buffered"] == 0


def test_flush_por_tabela_nao_toca_nas_outras(monkeypatch):
    module = _carregar_bigquery(monkeypatch, "bq_batch_tabela", BIGQUERY_BATCH_SIZE=100)
    chamadas = _espionar_bigquery(monkeypatch, module)

    module.enqueue_bigquery_row("proj.ds.a", {"i": 1})
    module.enqueue_bigquery_row("proj.ds.b", {"i": 2})
    module.flush_bigquery_batch_buffer("proj.ds.a")

    assert [tabela for tabela, _ in chamadas] == ["proj.ds.a"]
    assert module.get_bigquery_write_metrics()["rows_buffered"] == 1


def test_teto_do_buffer_desvia_para_a_dlq_em_vez_de_crescer(monkeypatch):
    """O buffer não pode crescer até derrubar o pod por memória.

    E o excedente não é descartado: vai para a DLQ, de onde é recuperável.
    """
    module = _carregar_bigquery(
        monkeypatch,
        "bq_batch_teto",
        BIGQUERY_BATCH_SIZE=1000,
        BIGQUERY_BATCH_MAX_BUFFERED_ROWS=3,
    )
    _espionar_bigquery(monkeypatch, module)

    desviados = []
    monkeypatch.setattr(
        module,
        "_persist_to_dlq",
        lambda tabela, linhas, erro: desviados.append((tabela, linhas, erro)),
    )

    for i in range(5):
        module.enqueue_bigquery_row("proj.ds.tbl", {"i": i})

    assert module.get_bigquery_write_metrics()["rows_buffered"] == 3
    assert sum(len(linhas) for _, linhas, _ in desviados) == 2
    # As mais antigas são as que saem.
    assert desviados[0][1][0] == {"i": 0}


# ---------------------------------------------------------------------------
# Encerramento
# ---------------------------------------------------------------------------


def test_sinal_de_encerramento_drena_o_buffer(monkeypatch):
    """O teste que prende a correção do CHATR-118.

    Antes, o buffer só era drenado por `atexit` — que não roda quando o
    processo morre por sinal com handler default, exatamente o que acontece com
    a uvicorn em SIGTERM. Cada rollout levava junto o que estivesse em memória.
    """
    module = _carregar_bigquery(monkeypatch, "bq_sinal", BIGQUERY_BATCH_SIZE=1000)
    chamadas = _espionar_bigquery(monkeypatch, module)

    module.enqueue_bigquery_row("proj.ds.tbl", {"i": 1})
    assert chamadas == []

    anterior_chamado = []
    module._previous_signal_handlers[signal.SIGTERM] = lambda *_a: (
        anterior_chamado.append(True)
    )

    module._handle_shutdown_signal(signal.SIGTERM, None)

    assert len(chamadas) == 1, "o buffer não foi drenado no sinal"
    assert chamadas[0][1] == [{"i": 1}]
    assert anterior_chamado == [True], "o handler anterior deixou de ser chamado"


def test_handler_de_sinal_nao_deixa_falha_de_flush_escapar(monkeypatch):
    """Encerramento nunca pode quebrar por causa do flush.

    Uma exceção aqui roubaria do handler anterior a chance de rodar, e o
    processo terminaria de um jeito que o Kubernetes lê como crash.
    """
    module = _carregar_bigquery(monkeypatch, "bq_sinal_erro")

    def _explode():
        raise RuntimeError("BigQuery fora do ar")

    monkeypatch.setattr(module, "_stop_batch_flush_thread", _explode)

    anterior_chamado = []
    module._previous_signal_handlers[signal.SIGTERM] = lambda *_a: (
        anterior_chamado.append(True)
    )

    module._handle_shutdown_signal(signal.SIGTERM, None)

    assert anterior_chamado == [True]


def test_recarregar_o_modulo_nao_encadeia_handlers(monkeypatch):
    """Duas cargas no mesmo processo não podem empilhar dois flushes."""
    primeiro = _carregar_bigquery(monkeypatch, "bq_sinal_1")
    handler_apos_primeira = signal.getsignal(signal.SIGTERM)
    segundo = _carregar_bigquery(monkeypatch, "bq_sinal_2")

    assert primeiro is not segundo
    assert signal.getsignal(signal.SIGTERM) is handler_apos_primeira


def test_escrita_de_background_roda_no_pool_dedicado(monkeypatch):
    """Escrita lenta não pode disputar thread com o resto do app.

    O retry de `insert_rows_json_with_retry_and_dlq` dorme entre as tentativas;
    no executor default essas threads são as mesmas de qualquer outra chamada
    bloqueante — o mesmo motivo que já havia justificado o pool de leitura.
    """
    import asyncio

    module = _carregar_bigquery(monkeypatch, "bq_pool_escrita")

    threads = []
    monkeypatch.setattr(
        module,
        "save_response_in_bq",
        lambda *_a, **_k: threads.append(threading.current_thread().name),
    )

    asyncio.run(
        module.save_response_in_bq_background(
            data={"x": 1}, endpoint="/t", dataset_id="ds", table_id="tbl"
        )
    )

    assert threads and threads[0].startswith("bq-write"), threads


# ---------------------------------------------------------------------------
# DLQ: teto, validade e sanitização
# ---------------------------------------------------------------------------


class _Pipeline:
    def __init__(self, registro, tamanho_apos_push=1):
        self._registro = registro
        self._comandos = []
        self._tamanho = tamanho_apos_push

    def rpush(self, *args):
        self._comandos.append(("rpush",) + args)
        return self

    def ltrim(self, *args):
        self._comandos.append(("ltrim",) + args)
        return self

    def expire(self, *args):
        self._comandos.append(("expire",) + args)
        return self

    def lpop(self, *args):
        self._comandos.append(("lpop",) + args)
        return self

    def execute(self):
        self._registro.extend(self._comandos)
        return [self._tamanho] + [True] * (len(self._comandos) - 1)


def test_dlq_grava_com_teto_e_validade(monkeypatch):
    """A DLQ divide instância com o cache: sem teto ela derruba o cache junto."""
    module = _carregar_bigquery(
        monkeypatch,
        "bq_dlq_teto",
        BIGQUERY_DLQ_MAX_ITEMS=250,
        BIGQUERY_DLQ_TTL_SECONDS=3600,
    )
    comandos = []
    monkeypatch.setattr(
        module,
        "_get_sync_redis_client",
        lambda: types.SimpleNamespace(pipeline=lambda: _Pipeline(comandos)),
    )

    module._persist_to_dlq("proj.ds.tbl", [{"a": 1}], "falhou")

    tipos = [c[0] for c in comandos]
    assert tipos == ["rpush", "ltrim", "expire"]
    assert comandos[1] == ("ltrim", "bq_dlq:proj.ds.tbl", -250, -1)
    assert comandos[2] == ("expire", "bq_dlq:proj.ds.tbl", 3600)


def test_estouro_do_teto_da_dlq_e_logado_como_critico(monkeypatch):
    """Descarte de DLQ é perda definitiva — não pode passar como aviso."""
    module = _carregar_bigquery(
        monkeypatch, "bq_dlq_estouro", BIGQUERY_DLQ_MAX_ITEMS=10
    )
    criticos = []
    monkeypatch.setattr(module.logger, "critical", lambda msg: criticos.append(msg))
    monkeypatch.setattr(
        module,
        "_get_sync_redis_client",
        # RPUSH devolvendo 12 com teto 10: dois itens foram descartados.
        lambda: types.SimpleNamespace(pipeline=lambda: _Pipeline([], 12)),
    )

    module._persist_to_dlq("proj.ds.tbl", [{"a": 1}], "falhou")

    assert criticos and "DESCARTADOS" in criticos[0]


@pytest.mark.parametrize(
    "nome, esperado",
    [
        ("../../etc/passwd", ".._.._etc_passwd"),
        ("proj.ds.tbl", "proj.ds.tbl"),
        ("tbl;rm -rf /", "tbl_rm_-rf__"),
    ],
)
def test_nome_de_tabela_e_sanitizado(monkeypatch, nome, esperado):
    """O nome vira caminho de arquivo e chave de Redis — travessia fecha aqui."""
    module = _carregar_bigquery(monkeypatch, f"bq_sanit_{abs(hash(nome))}")
    assert module._sanitize_table_name(nome) == esperado


def test_arquivo_da_dlq_nao_escapa_do_diretorio(monkeypatch, tmp_path):
    module = _carregar_bigquery(
        monkeypatch, "bq_dlq_path", DATA_DIR=str(tmp_path), REDIS_URL=None
    )
    caminho = module._dlq_file_path("../../etc/passwd")
    assert caminho.parent == tmp_path / "bq_dlq"
    assert caminho.resolve().is_relative_to((tmp_path / "bq_dlq").resolve())


# ---------------------------------------------------------------------------
# DLQ: reprocessamento
# ---------------------------------------------------------------------------


class _RedisFalso:
    """Redis em memória com o subconjunto de comandos que o drain usa."""

    def __init__(self, listas=None):
        self.listas = {k: list(v) for k, v in (listas or {}).items()}
        self.strings = {}

    def set(self, chave, valor, nx=False, ex=None):
        if nx and chave in self.strings:
            return None
        self.strings[chave] = valor
        return True

    def get(self, chave):
        return self.strings.get(chave)

    def delete(self, chave):
        self.strings.pop(chave, None)
        self.listas.pop(chave, None)
        return 1

    def scan_iter(self, match=None, count=None):
        prefixo = (match or "*").rstrip("*")
        return [c for c in list(self.listas) if c.startswith(prefixo)]

    def lrange(self, chave, inicio, fim):
        lista = self.listas.get(chave, [])
        return lista[inicio : (None if fim == -1 else fim + 1)]

    def lpop(self, chave):
        lista = self.listas.get(chave, [])
        return lista.pop(0) if lista else None

    def llen(self, chave):
        return len(self.listas.get(chave, []))

    def rpush(self, chave, valor):
        self.listas.setdefault(chave, []).append(valor)
        return len(self.listas[chave])

    def pipeline(self):
        return _PipelineReal(self)


class _PipelineReal:
    """Pipeline que aplica de verdade no `_RedisFalso`, na ordem enfileirada."""

    def __init__(self, redis):
        self._redis = redis
        self._ops = []

    def rpush(self, chave, valor):
        self._ops.append(lambda: self._redis.rpush(chave, valor))
        return self

    def ltrim(self, chave, inicio, fim):
        def _aplicar():
            lista = self._redis.listas.get(chave, [])
            self._redis.listas[chave] = lista[inicio:] if fim == -1 else lista
            return True

        self._ops.append(_aplicar)
        return self

    def expire(self, *_args):
        self._ops.append(lambda: True)
        return self

    def lpop(self, chave):
        self._ops.append(lambda: self._redis.lpop(chave))
        return self

    def execute(self):
        return [op() for op in self._ops]


def _item_dlq(tabela="proj.ds.tbl", payload=None):
    return json.dumps(
        {
            "table_full_name": tabela,
            "failed_at": "2026-08-20T10:00:00.000000",
            "error": "falha anterior",
            "payload": payload if payload is not None else [{"a": 1}],
        }
    )


def test_replay_devolve_o_item_ao_bigquery_e_esvazia_a_fila(monkeypatch):
    """O critério de aceite do CHATR-126: o payload volta para a tabela."""
    module = _carregar_bigquery(monkeypatch, "bq_replay_ok")
    chamadas = _espionar_bigquery(monkeypatch, module)
    redis = _RedisFalso({"bq_dlq:proj.ds.tbl": [_item_dlq()]})
    monkeypatch.setattr(module, "_get_sync_redis_client", lambda: redis)

    resumo = module._replay_dlq_redis(limite=10)

    assert resumo["itens"] == 1
    assert resumo["linhas"] == 1
    assert resumo["pendentes"] == 0
    assert chamadas == [("proj.ds.tbl", [{"a": 1}])]
    assert redis.listas["bq_dlq:proj.ds.tbl"] == []
    assert module.get_bigquery_write_metrics()["rows_replayed"] == 1


def test_falha_transitoria_mantem_o_item_na_fila(monkeypatch):
    """Se o BigQuery ainda está fora, o item não pode sair da DLQ."""
    module = _carregar_bigquery(monkeypatch, "bq_replay_transitorio")
    _espionar_bigquery(monkeypatch, module, erro=ServiceUnavailable("503"))
    redis = _RedisFalso({"bq_dlq:proj.ds.tbl": [_item_dlq()]})
    monkeypatch.setattr(module, "_get_sync_redis_client", lambda: redis)

    resumo = module._replay_dlq_redis(limite=10)

    assert resumo["itens"] == 0
    assert resumo["erros"]
    assert len(redis.listas["bq_dlq:proj.ds.tbl"]) == 1, "o item foi perdido"


def test_payload_recusado_para_sempre_sai_da_frente(monkeypatch):
    """Um registro malformado não pode bloquear a fila inteira atrás dele."""
    module = _carregar_bigquery(monkeypatch, "bq_replay_poison")
    _espionar_bigquery(monkeypatch, module, erro=BadRequest("schema inválido"))
    redis = _RedisFalso(
        {"bq_dlq:proj.ds.tbl": [_item_dlq(), _item_dlq(payload=[{"b": 2}])]}
    )
    monkeypatch.setattr(module, "_get_sync_redis_client", lambda: redis)

    resumo = module._replay_dlq_redis(limite=10)

    assert resumo["poison"] == 2
    assert redis.listas["bq_dlq:proj.ds.tbl"] == []
    # Não descartado: guardado à parte para correção manual.
    assert len(redis.listas["bq_dlq_poison:proj.ds.tbl"]) == 2


def test_item_ilegivel_vai_para_poison_em_vez_de_travar(monkeypatch):
    module = _carregar_bigquery(monkeypatch, "bq_replay_ilegivel")
    _espionar_bigquery(monkeypatch, module)
    redis = _RedisFalso({"bq_dlq:proj.ds.tbl": ["{isso não é json", _item_dlq()]})
    monkeypatch.setattr(module, "_get_sync_redis_client", lambda: redis)

    resumo = module._replay_dlq_redis(limite=10)

    assert resumo["poison"] == 1
    assert resumo["itens"] == 1, "o item válido atrás do ilegível não foi processado"


def test_replay_respeita_o_limite(monkeypatch):
    module = _carregar_bigquery(monkeypatch, "bq_replay_limite")
    chamadas = _espionar_bigquery(monkeypatch, module)
    redis = _RedisFalso({"bq_dlq:proj.ds.tbl": [_item_dlq() for _ in range(5)]})
    monkeypatch.setattr(module, "_get_sync_redis_client", lambda: redis)

    resumo = module._replay_dlq_redis(limite=2)

    assert resumo["itens"] == 2
    assert len(chamadas) == 2
    assert resumo["pendentes"] == 3


def test_dry_run_nao_consome_a_fila(monkeypatch):
    """Conferir o que está parado não pode alterar nada."""
    module = _carregar_bigquery(monkeypatch, "bq_replay_dryrun")
    chamadas = _espionar_bigquery(monkeypatch, module)
    redis = _RedisFalso({"bq_dlq:proj.ds.tbl": [_item_dlq(), _item_dlq()]})
    monkeypatch.setattr(module, "_get_sync_redis_client", lambda: redis)

    resumo = module._replay_dlq_redis(limite=10, dry_run=True)

    assert chamadas == []
    assert len(redis.listas["bq_dlq:proj.ds.tbl"]) == 2
    assert resumo["pendentes"] == 2


def test_drain_concorrente_e_serializado_pelo_lock(monkeypatch):
    """Duas réplicas drenando ao mesmo tempo duplicariam as linhas."""
    module = _carregar_bigquery(monkeypatch, "bq_replay_lock")
    _espionar_bigquery(monkeypatch, module)
    redis = _RedisFalso({"bq_dlq:proj.ds.tbl": [_item_dlq()]})
    redis.strings[module._DLQ_DRAIN_LOCK_KEY] = "outra-replica"
    monkeypatch.setattr(module, "_get_sync_redis_client", lambda: redis)

    resumo = module._replay_dlq_redis(limite=10)

    assert resumo["itens"] == 0
    assert len(redis.listas["bq_dlq:proj.ds.tbl"]) == 1
    assert redis.strings[module._DLQ_DRAIN_LOCK_KEY] == "outra-replica"


def test_lock_e_liberado_ao_fim_do_drain(monkeypatch):
    module = _carregar_bigquery(monkeypatch, "bq_replay_lock_livre")
    _espionar_bigquery(monkeypatch, module)
    redis = _RedisFalso({"bq_dlq:proj.ds.tbl": [_item_dlq()]})
    monkeypatch.setattr(module, "_get_sync_redis_client", lambda: redis)

    module._replay_dlq_redis(limite=10)

    assert module._DLQ_DRAIN_LOCK_KEY not in redis.strings


# ---------------------------------------------------------------------------
# DLQ em arquivo
# ---------------------------------------------------------------------------


def test_replay_de_arquivo_reprocessa_e_remove(monkeypatch, tmp_path):
    module = _carregar_bigquery(
        monkeypatch, "bq_replay_arquivo", DATA_DIR=str(tmp_path), REDIS_URL=None
    )
    chamadas = _espionar_bigquery(monkeypatch, module)

    dlq_dir = tmp_path / "bq_dlq"
    dlq_dir.mkdir()
    (dlq_dir / "dlq_proj_ds_tbl.jsonl").write_text(
        _item_dlq() + "\n" + _item_dlq(payload=[{"b": 2}]) + "\n", encoding="utf-8"
    )

    resumo = module._replay_dlq_arquivos(limite=10)

    assert resumo["itens"] == 2
    assert len(chamadas) == 2
    assert not (dlq_dir / "dlq_proj_ds_tbl.jsonl").exists()
    assert not list(dlq_dir.glob("*.processing"))


def test_replay_de_arquivo_preserva_o_que_nao_conseguiu_escrever(monkeypatch, tmp_path):
    """Falha transitória tem que deixar o registro em disco, não sumir com ele."""
    module = _carregar_bigquery(
        monkeypatch, "bq_replay_arquivo_falha", DATA_DIR=str(tmp_path), REDIS_URL=None
    )
    _espionar_bigquery(monkeypatch, module, erro=ServiceUnavailable("503"))

    dlq_dir = tmp_path / "bq_dlq"
    dlq_dir.mkdir()
    (dlq_dir / "dlq_proj_ds_tbl.jsonl").write_text(_item_dlq() + "\n", encoding="utf-8")

    resumo = module._replay_dlq_arquivos(limite=10)

    assert resumo["itens"] == 0
    assert resumo["pendentes"] == 1
    sobras = list(dlq_dir.glob("dlq_proj_ds_tbl.jsonl*"))
    assert sobras, "o registro foi perdido no reprocessamento"
    assert _item_dlq() in sobras[0].read_text(encoding="utf-8")


def test_arquivo_processing_orfao_e_adotado(monkeypatch, tmp_path):
    """Sobra de uma execução que morreu no meio não pode envelhecer sozinha."""
    module = _carregar_bigquery(
        monkeypatch, "bq_replay_orfao", DATA_DIR=str(tmp_path), REDIS_URL=None
    )
    chamadas = _espionar_bigquery(monkeypatch, module)

    dlq_dir = tmp_path / "bq_dlq"
    dlq_dir.mkdir()
    (dlq_dir / "dlq_proj_ds_tbl.jsonl.processing").write_text(
        _item_dlq() + "\n", encoding="utf-8"
    )

    resumo = module._replay_dlq_arquivos(limite=10)

    assert resumo["itens"] == 1
    assert len(chamadas) == 1
    assert not list(dlq_dir.glob("*.processing"))


# ---------------------------------------------------------------------------
# Profundidade
# ---------------------------------------------------------------------------


def test_profundidade_soma_redis_poison_e_arquivos(monkeypatch, tmp_path):
    """Sem esta medida ninguém descobre que há dado parado."""
    module = _carregar_bigquery(monkeypatch, "bq_profundidade", DATA_DIR=str(tmp_path))
    redis = _RedisFalso(
        {
            "bq_dlq:proj.ds.a": [_item_dlq(), _item_dlq()],
            "bq_dlq_poison:proj.ds.b": [_item_dlq()],
        }
    )
    monkeypatch.setattr(module, "_get_sync_redis_client", lambda: redis)

    dlq_dir = tmp_path / "bq_dlq"
    dlq_dir.mkdir()
    (dlq_dir / "dlq_proj_ds_c.jsonl").write_text(_item_dlq() + "\n", encoding="utf-8")

    profundidade = module.get_dlq_depth()

    assert profundidade["redis"] == 2
    assert profundidade["poison"] == 1
    assert profundidade["arquivos"] == 1
    assert profundidade["total"] == 4


def test_profundidade_zero_com_dlq_vazia(monkeypatch, tmp_path):
    module = _carregar_bigquery(
        monkeypatch, "bq_profundidade_zero", DATA_DIR=str(tmp_path)
    )
    monkeypatch.setattr(module, "_get_sync_redis_client", lambda: _RedisFalso())
    assert module.get_dlq_depth()["total"] == 0


# ---------------------------------------------------------------------------
# Classificação de falha
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "erro, permanente",
    [
        (BadRequest("schema"), True),
        (ServiceUnavailable("503"), False),
        (TimeoutError("socket"), False),
    ],
)
def test_classificacao_de_falha(monkeypatch, erro, permanente):
    module = _carregar_bigquery(monkeypatch, f"bq_classif_{type(erro).__name__}")
    assert module._e_falha_permanente(erro) is permanente


def test_linha_recusada_por_schema_nao_e_retentada(monkeypatch):
    """Repetir linha malformada só segura a thread do pool de escrita."""
    module = _carregar_bigquery(monkeypatch, "bq_sem_retry_schema")
    chamadas = []

    def _insert_rows_json(tabela, linhas, **_kwargs):
        chamadas.append(tabela)
        return [{"index": 0, "errors": [{"reason": "invalid"}]}]

    monkeypatch.setattr(
        module,
        "get_bigquery_client",
        lambda: types.SimpleNamespace(insert_rows_json=_insert_rows_json),
    )

    with pytest.raises(module.BigQueryRowRejectedError):
        module._insert_rows_json_raw("proj.ds.tbl", [{"a": 1}], max_retries=3)

    assert len(chamadas) == 1, "houve retry de uma falha permanente"


# ---------------------------------------------------------------------------
# Worker de drain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_de_drain_sobrevive_a_erro_inesperado(monkeypatch):
    """O laço é a única coisa que devolve dado à tabela de destino.

    Deixar uma exceção escapar o mataria em silêncio, e a DLQ voltaria a ser um
    depósito sem saída — o defeito que o CHATR-126 existe para corrigir.
    """
    import asyncio

    module = _carregar_bigquery(
        monkeypatch, "bq_drain_erro", BIGQUERY_DLQ_DRAIN_INTERVAL_SECONDS=0.01
    )

    tentativas = []

    def _replay():
        tentativas.append(True)
        if len(tentativas) == 1:
            raise RuntimeError("Redis caiu no meio da varredura")
        return {"itens": 0, "linhas": 0, "poison": 0, "pendentes": 0, "erros": []}

    monkeypatch.setattr(module, "replay_bigquery_dlq", _replay)

    task = asyncio.create_task(module.drain_bigquery_dlq_loop())
    for _ in range(200):
        await asyncio.sleep(0.01)
        if len(tentativas) >= 2:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(tentativas) >= 2, "o worker morreu na primeira exceção"


@pytest.mark.asyncio
async def test_worker_de_drain_encerra_no_cancelamento(monkeypatch):
    """Task pendurada no shutdown deixaria o lifespan travado."""
    import asyncio

    module = _carregar_bigquery(
        monkeypatch, "bq_drain_cancel", BIGQUERY_DLQ_DRAIN_INTERVAL_SECONDS=60
    )
    task = asyncio.create_task(module.drain_bigquery_dlq_loop())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


@pytest.mark.asyncio
async def test_drain_nao_bloqueia_a_event_loop(monkeypatch):
    """Redis síncrono e insert do BigQuery precisam sair para uma thread.

    Se a varredura rodasse na event loop, uma DLQ cheia congelaria o servidor
    inteiro enquanto ela escoa.
    """
    import asyncio

    module = _carregar_bigquery(
        monkeypatch, "bq_drain_thread", BIGQUERY_DLQ_DRAIN_INTERVAL_SECONDS=0.01
    )

    threads = []

    def _replay():
        threads.append(threading.current_thread().name)
        return {"itens": 0, "linhas": 0, "poison": 0, "pendentes": 0, "erros": []}

    monkeypatch.setattr(module, "replay_bigquery_dlq", _replay)

    task = asyncio.create_task(module.drain_bigquery_dlq_loop())
    for _ in range(200):
        await asyncio.sleep(0.01)
        if threads:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert threads and threads[0].startswith("bq-write"), threads


def test_insert_leva_teto_de_tempo(monkeypatch):
    """Chamada sem teto pendura o handler de sinal até o SIGKILL.

    É o caminho do flush de encerramento: sem `timeout`, `insert_rows_json`
    herda o default do transporte e pode não voltar — e aí não sobra nem o que
    a DLQ salvaria.
    """
    module = _carregar_bigquery(
        monkeypatch, "bq_timeout_escrita", BIGQUERY_WRITE_TIMEOUT_SECONDS=7.5
    )
    recebidos = []

    def _insert_rows_json(tabela, linhas, **kwargs):
        recebidos.append(kwargs)
        return []

    monkeypatch.setattr(
        module,
        "get_bigquery_client",
        lambda: types.SimpleNamespace(insert_rows_json=_insert_rows_json),
    )

    module._insert_rows_json_raw("proj.ds.tbl", [{"a": 1}])

    assert recebidos == [{"timeout": 7.5}]


def test_flush_de_encerramento_usa_teto_mais_curto(monkeypatch):
    """No shutdown o orçamento é o `terminationGracePeriod`, não a escrita."""
    module = _carregar_bigquery(
        monkeypatch,
        "bq_timeout_shutdown",
        BIGQUERY_BATCH_SIZE=1000,
        BIGQUERY_WRITE_TIMEOUT_SECONDS=30.0,
        BIGQUERY_SHUTDOWN_TIMEOUT_SECONDS=2.0,
    )
    recebidos = []

    def _insert_rows_json(tabela, linhas, **kwargs):
        recebidos.append(kwargs)
        return []

    monkeypatch.setattr(
        module,
        "get_bigquery_client",
        lambda: types.SimpleNamespace(insert_rows_json=_insert_rows_json),
    )

    module.enqueue_bigquery_row("proj.ds.tbl", {"i": 1})
    module._stop_batch_flush_thread()

    assert recebidos == [{"timeout": 2.0}]


def test_falha_ao_mover_para_poison_nao_vira_laco_infinito(monkeypatch):
    """Item que não sai da cabeça da lista prenderia a thread para sempre.

    O laço relê a cabeça a cada iteração: sem tratar a remoção que falha, o
    mesmo item seria reprocessado indefinidamente, segurando uma thread do pool
    de escrita e impedindo o worker de drain de voltar.
    """
    module = _carregar_bigquery(monkeypatch, "bq_poison_travado")
    _espionar_bigquery(monkeypatch, module, erro=BadRequest("schema"))
    redis = _RedisFalso({"bq_dlq:proj.ds.tbl": [_item_dlq(), _item_dlq()]})
    monkeypatch.setattr(module, "_get_sync_redis_client", lambda: redis)
    monkeypatch.setattr(
        module,
        "_mover_para_poison",
        lambda *_a: False,  # Redis recusa a remoção
    )

    resumo = module._replay_dlq_redis(limite=10)

    assert resumo["erros"], "a falha de remoção não foi reportada"
    assert len(redis.listas["bq_dlq:proj.ds.tbl"]) == 2, "o item foi perdido"


def test_item_sem_payload_e_descartado_sem_travar(monkeypatch):
    """Registro vazio não tem o que reprocessar, mas não pode bloquear a fila."""
    module = _carregar_bigquery(monkeypatch, "bq_payload_vazio")
    chamadas = _espionar_bigquery(monkeypatch, module)
    redis = _RedisFalso({"bq_dlq:proj.ds.tbl": [_item_dlq(payload=[]), _item_dlq()]})
    monkeypatch.setattr(module, "_get_sync_redis_client", lambda: redis)

    resumo = module._replay_dlq_redis(limite=10)

    assert resumo["itens"] == 1
    assert len(chamadas) == 1
    assert redis.listas["bq_dlq:proj.ds.tbl"] == []
