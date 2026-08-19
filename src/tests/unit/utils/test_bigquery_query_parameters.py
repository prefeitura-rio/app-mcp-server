"""Repasse de parâmetros nomeados ao client do BigQuery (CHATR-117).

`pluscode_service` monta `ScalarQueryParameter`/`ArrayQueryParameter` em vez de
interpolar valores no SQL, mas isso só protege de fato se `get_bigquery_result`
converter esses objetos em `QueryJobConfig` e entregá-los ao client. Este arquivo
cobre esse trecho de `_execute_bigquery_query`, que os demais testes de BigQuery
não exercem porque todos chamam a função sem parâmetros.

O `bigquery` real do google-cloud é usado de propósito: o objeto sob teste é o
`QueryJobConfig` de verdade, com o round-trip que ele faz pela representação de
API. Só o client é dublê.
"""

import base64
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest
from google.cloud import bigquery


PROJECT_ROOT = Path(__file__).resolve().parents[4]

_SQL = "select * from `t` where plus8 = @plus8 and categoria in unnest(@categories)"

# Valor hostil: se algum dia o texto da query passar a carregar os valores em vez
# dos parâmetros, é ele que aparece no SQL e faz o teste falhar.
_CATEGORIAS_HOSTIS = ["SAUDE') OR 1=1 --", "EDUCACAO"]

_PARAMETROS = [
    bigquery.ScalarQueryParameter("plus8", "STRING", "589R2QCH"),
    bigquery.ArrayQueryParameter("categories", "STRING", _CATEGORIAS_HOSTIS),
]


def _ensure_package(name: str, path: Path) -> types.ModuleType:
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(path)]
    sys.modules[name] = pkg
    return pkg


def _passthrough_interceptor(*_args, **_kwargs):
    def decorator(func):
        return func

    return decorator


def _load_bigquery_module(monkeypatch, module_alias: str) -> types.ModuleType:
    """Carrega `src/utils/bigquery.py` isolado, sob um alias próprio.

    Mesma abordagem de `test_bigquery_client_caching.py`: o `env` é um dublê sem
    `REDIS_URL`, então a camada de cache fica inerte e as queries chegam sempre
    ao client — que é o que estes testes observam.
    """
    _ensure_package("src", PROJECT_ROOT / "src")
    _ensure_package("src.config", PROJECT_ROOT / "src" / "config")
    _ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")

    env_module = types.SimpleNamespace(
        GCP_SERVICE_ACCOUNT_CREDENTIALS=base64.b64encode(
            json.dumps({"project_id": "proj-params-test"}).encode()
        ).decode(),
        GOOGLE_BIGQUERY_PAGE_SIZE=100,
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


class _LinhaFalsa(dict):
    """Linha do BigQuery: o código itera sobre `.items()`."""


class _JobFalso:
    def __init__(self, linhas):
        self._linhas = linhas

    def result(self, page_size=None, timeout=None):
        return self._linhas


class _ClienteFalso:
    """Registra o que `_execute_bigquery_query` entrega ao client do BigQuery."""

    def __init__(self, linhas=None):
        self.chamadas = []
        self._linhas = linhas if linhas is not None else [_LinhaFalsa({"n": 1})]

    def query(self, query, **kwargs):
        self.chamadas.append((query, kwargs))
        return _JobFalso(self._linhas)


def _cliente_registrado(monkeypatch, module) -> _ClienteFalso:
    cliente = _ClienteFalso()
    monkeypatch.setattr(module, "get_bigquery_client", lambda: cliente)
    return cliente


# ---------------------------------------------------------------------------
# Testes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parametros_viram_query_job_config(monkeypatch):
    """Os parâmetros recebidos chegam ao client dentro de um `QueryJobConfig`."""
    module = _load_bigquery_module(monkeypatch, "bq_params_job_config")
    cliente = _cliente_registrado(monkeypatch, module)

    await module.get_bigquery_result(
        _SQL, query_parameters=_PARAMETROS, cache_ttl_seconds=0
    )

    _query, kwargs = cliente.chamadas[0]
    job_config = kwargs["job_config"]
    assert isinstance(job_config, bigquery.QueryJobConfig)
    # `QueryJobConfig` serializa os parâmetros para a representação de API e os
    # reconstrói na leitura: os objetos não são os mesmos, têm de ser iguais.
    assert job_config.query_parameters == _PARAMETROS


@pytest.mark.asyncio
async def test_valores_dos_parametros_nao_entram_no_texto_da_query(monkeypatch):
    """O SQL entregue ao client é idêntico ao recebido — nada é interpolado nele."""
    module = _load_bigquery_module(monkeypatch, "bq_params_sql_intacto")
    cliente = _cliente_registrado(monkeypatch, module)

    await module.get_bigquery_result(
        _SQL, query_parameters=_PARAMETROS, cache_ttl_seconds=0
    )

    query, _kwargs = cliente.chamadas[0]
    assert query == _SQL
    assert "OR 1=1" not in query
    assert "589R2QCH" not in query


@pytest.mark.asyncio
async def test_array_parameter_preserva_valores_ate_o_client(monkeypatch):
    """A lista de categorias chega inteira, sem escaping nem reordenação."""
    module = _load_bigquery_module(monkeypatch, "bq_params_array")
    cliente = _cliente_registrado(monkeypatch, module)

    await module.get_bigquery_result(
        _SQL, query_parameters=_PARAMETROS, cache_ttl_seconds=0
    )

    _query, kwargs = cliente.chamadas[0]
    por_nome = {p.name: p for p in kwargs["job_config"].query_parameters}
    assert list(por_nome["categories"].values) == _CATEGORIAS_HOSTIS
    assert por_nome["categories"].array_type == "STRING"
    assert por_nome["plus8"].value == "589R2QCH"


@pytest.mark.asyncio
@pytest.mark.parametrize("sem_parametros", [None, []])
async def test_query_sem_parametros_nao_monta_job_config(monkeypatch, sem_parametros):
    """Sem parâmetros o client é chamado só com o SQL, sem config vazia."""
    module = _load_bigquery_module(monkeypatch, "bq_params_sem_config")
    cliente = _cliente_registrado(monkeypatch, module)

    await module.get_bigquery_result(
        "select 1", query_parameters=sem_parametros, cache_ttl_seconds=0
    )

    query, kwargs = cliente.chamadas[0]
    assert query == "select 1"
    assert "job_config" not in kwargs
