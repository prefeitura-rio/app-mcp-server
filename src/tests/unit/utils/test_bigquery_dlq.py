"""Cliente Redis síncrono da DLQ de escrita (CHATR-102).

A branch deu timeout de socket ao cliente async do cache e deixou o síncrono
como estava. A assimetria importa mais do lado síncrono, não menos: este
cliente roda em thread do executor default, chamado por `_persist_to_dlq` no
caminho de falha de escrita. Sem timeout, um Redis que aceita a conexão e para
de responder prende a thread para sempre — e, como a chamada nunca retorna, o
fallback em arquivo logo abaixo (que existe justamente para quando o Redis não
está disponível) nunca chega a rodar. Medido antes da correção: ainda preso
após 8s, sem teto.

Os testes abaixo verificam que o timeout é configurável por ambiente, que tem
default, e — o que de fato importa — que o fallback em arquivo é alcançado
quando o Redis falha.
"""

import base64
import importlib.util
import json
import sys
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

    env_module = types.SimpleNamespace(
        GCP_SERVICE_ACCOUNT_CREDENTIALS=base64.b64encode(
            json.dumps({"project_id": "proj-dlq-test"}).encode()
        ).decode(),
        GOOGLE_BIGQUERY_PAGE_SIZE=100,
        BIGQUERY_CACHE_TTL_SECONDS=3600,
        BIGQUERY_TIMEOUT_SECONDS=10.0,
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


class _PipelineFake:
    """Pipeline de Redis que registra os comandos em vez de executá-los.

    `_persist_to_dlq` grava payload, teto (`LTRIM`) e validade (`EXPIRE`) numa
    pipeline só — um fake com apenas `rpush` não representa mais o contrato.
    """

    def __init__(self, registro, tamanho_apos_push=1):
        self._registro = registro
        self._comandos = []
        self._tamanho = tamanho_apos_push

    def rpush(self, chave, valor):
        self._comandos.append(("rpush", chave, valor))
        return self

    def ltrim(self, chave, inicio, fim):
        self._comandos.append(("ltrim", chave, inicio, fim))
        return self

    def expire(self, chave, ttl):
        self._comandos.append(("expire", chave, ttl))
        return self

    def execute(self):
        self._registro.extend(self._comandos)
        # O primeiro retorno é o do RPUSH: o tamanho da lista após o push, que
        # é como `_persist_to_dlq` descobre se o teto descartou item.
        return [self._tamanho] + [True] * (len(self._comandos) - 1)


def _redis_fake(registro, tamanho_apos_push=1):
    return types.SimpleNamespace(
        pipeline=lambda: _PipelineFake(registro, tamanho_apos_push)
    )


def _capturar_from_url(monkeypatch, module):
    """Troca `redis.Redis.from_url` por um espião dos kwargs recebidos."""
    import redis as redis_sync

    chamadas = []

    def _from_url(url, **kwargs):
        chamadas.append((url, kwargs))
        return _redis_fake([])

    monkeypatch.setattr(redis_sync.Redis, "from_url", staticmethod(_from_url))
    module._sync_redis_client = None
    return chamadas


# ---------------------------------------------------------------------------
# Timeout de socket
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "campo", ["socket_timeout", "socket_connect_timeout"], ids=["leitura", "conexao"]
)
def test_cliente_sincrono_e_criado_com_timeout_de_socket(monkeypatch, campo):
    """O assert que prende a correção: sem isto a thread fica presa sem teto."""
    module = _load_bigquery_module(
        monkeypatch,
        "bq_dlq_timeout",
        REDIS_URL="redis://localhost:6379/0",
        REDIS_DLQ_TIMEOUT_SECONDS=7.5,
    )
    chamadas = _capturar_from_url(monkeypatch, module)

    module._get_sync_redis_client()

    _url, kwargs = chamadas[0]
    assert kwargs[campo] == 7.5


def test_timeout_da_dlq_tem_default_quando_o_env_nao_traz(monkeypatch):
    """Ambiente antigo, sem a variável nova, não pode voltar a ficar sem teto."""
    module = _load_bigquery_module(
        monkeypatch, "bq_dlq_default", REDIS_URL="redis://localhost:6379/0"
    )
    chamadas = _capturar_from_url(monkeypatch, module)

    module._get_sync_redis_client()

    _url, kwargs = chamadas[0]
    assert kwargs["socket_timeout"] == 5.0
    assert kwargs["socket_connect_timeout"] == 5.0


def test_dlq_espera_mais_que_o_cache(monkeypatch):
    """Os dois timeouts existem separados porque a escolha é diferente.

    O cache que desiste cedo cai para o BigQuery, que é a fonte da verdade. A
    DLQ que desiste cedo cai para arquivo local — em pod efêmero, isso é perder
    o registro no próximo restart. Vale esperar mais, desde que haja teto.
    """
    import src.config.env as env_real

    assert env_real.REDIS_DLQ_TIMEOUT_SECONDS > env_real.REDIS_CACHE_TIMEOUT_SECONDS


def test_cliente_sincrono_e_reaproveitado(monkeypatch):
    """Singleton: uma construção por processo, como o client do BigQuery."""
    module = _load_bigquery_module(
        monkeypatch, "bq_dlq_singleton", REDIS_URL="redis://localhost:6379/0"
    )
    chamadas = _capturar_from_url(monkeypatch, module)

    primeiro = module._get_sync_redis_client()
    segundo = module._get_sync_redis_client()

    assert primeiro is segundo
    assert len(chamadas) == 1


def test_sem_redis_url_o_cliente_sincrono_fica_none(monkeypatch):
    """Ambiente sem Redis não pode quebrar a escrita — só perde a DLQ Redis."""
    module = _load_bigquery_module(monkeypatch, "bq_dlq_sem_url")

    assert module._get_sync_redis_client() is None


# ---------------------------------------------------------------------------
# O que o timeout existe para viabilizar: o fallback em arquivo
# ---------------------------------------------------------------------------


def test_falha_do_redis_cai_para_o_arquivo(monkeypatch, tmp_path):
    """Sem teto no socket esta linha nunca era alcançada: a chamada não voltava."""
    module = _load_bigquery_module(
        monkeypatch,
        "bq_dlq_fallback",
        REDIS_URL="redis://localhost:6379/0",
        DATA_DIR=str(tmp_path),
    )

    def _redis_que_falha():
        class _PipelineQueTrava(_PipelineFake):
            def execute(self):
                raise TimeoutError("Timeout reading from localhost:6379")

        return types.SimpleNamespace(pipeline=lambda: _PipelineQueTrava([]))

    monkeypatch.setattr(module, "_get_sync_redis_client", _redis_que_falha)

    module._persist_to_dlq("proj.ds.tbl", [{"a": 1}], "erro de escrita")

    arquivo = tmp_path / "bq_dlq" / "dlq_proj_ds_tbl.jsonl"
    assert arquivo.exists()
    registro = json.loads(arquivo.read_text(encoding="utf-8").strip())
    assert registro["table_full_name"] == "proj.ds.tbl"
    assert registro["payload"] == [{"a": 1}]
    assert registro["error"] == "erro de escrita"


def test_redis_disponivel_nao_escreve_arquivo(monkeypatch, tmp_path):
    """O arquivo é fallback, não cópia: com Redis de pé, nada em disco."""
    module = _load_bigquery_module(
        monkeypatch,
        "bq_dlq_sem_arquivo",
        REDIS_URL="redis://localhost:6379/0",
        DATA_DIR=str(tmp_path),
    )
    comandos = []
    monkeypatch.setattr(module, "_get_sync_redis_client", lambda: _redis_fake(comandos))

    module._persist_to_dlq("proj.ds.tbl", [{"a": 1}], "erro de escrita")

    assert comandos[0][0] == "rpush"
    assert comandos[0][1] == "bq_dlq:proj.ds.tbl"
    assert not (tmp_path / "bq_dlq").exists()
