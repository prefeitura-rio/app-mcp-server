"""Contrato de exceções da leitura do BigQuery — nenhuma no limbo.

A leitura tem quatro desfechos de falha, e o chamador precisa conseguir
separá-los sem ler mensagem de erro:

* `NotFound` — tabela ausente. Degrada para lista vazia, sem cachear.
* `GoogleAPIError` — falha conhecida de infraestrutura (400 de tabela externa
  sobre Google Sheet, 403 do Drive). Sobe com o tipo original.
* `TimeoutError` — estourou o prazo. **Qualquer** dos dois relógios: o
  `asyncio.wait_for` do chamador e o `query_job.result(timeout=)` que roda
  dentro da thread.
* `BigQueryQueryError` — o resto, que normalmente é bug nosso ou mudança de API.

O caso que faltava era o terceiro. `.result(timeout=)` levanta
`concurrent.futures.TimeoutError` (que em 3.11+ é o `TimeoutError` embutido),
e ele caía no `except Exception` genérico, sendo re-levantado como uma
`Exception` crua com a mensagem "Failed to execute BigQuery query". Resultado:
a mesma condição — query lenta — chegava ao chamador como `TimeoutError` ou
como `Exception` conforme qual relógio disparasse primeiro. Como os dois usam
o mesmo prazo, isso era decidido por corrida.

O client dublê dos testes de timeout registrava o kwarg `timeout` mas nunca
levantava nada, então a suíte nunca passou por esse ramo.
"""

import asyncio
import base64
import concurrent.futures
import importlib.util
import json
import sys
import threading
import types
from pathlib import Path

import pytest
from google.api_core.exceptions import (
    BadRequest,
    Forbidden,
    GoogleAPIError,
    NotFound,
)


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
            json.dumps({"project_id": "proj-exc-test"}).encode()
        ).decode(),
        "GOOGLE_BIGQUERY_PAGE_SIZE": 100,
        "BIGQUERY_TIMEOUT_SECONDS": 10.0,
        "BIGQUERY_CACHE_TTL_SECONDS": 3600,
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


class _Job:
    """Job cujo `.result()` e `.cancel()` são configuráveis pelo teste."""

    def __init__(self, erro=None, linhas=None, erro_no_cancel=None):
        self.erro = erro
        self.linhas = linhas if linhas is not None else []
        self.erro_no_cancel = erro_no_cancel
        self.cancelado = False
        self.thread_da_execucao = None

    def result(self, page_size=None, timeout=None):
        self.thread_da_execucao = threading.current_thread().name
        if self.erro is not None:
            raise self.erro
        return self.linhas

    def cancel(self):
        self.cancelado = True
        if self.erro_no_cancel is not None:
            raise self.erro_no_cancel


class _Cliente:
    def __init__(self, job=None, erro_na_submissao=None):
        self.job = job if job is not None else _Job()
        self.erro_na_submissao = erro_na_submissao

    def query(self, _query, **_kwargs):
        if self.erro_na_submissao is not None:
            raise self.erro_na_submissao
        return self.job


def _montar(monkeypatch, alias, cliente=None, **env_extra):
    module = _load_bigquery_module(monkeypatch, alias, **env_extra)
    cliente = cliente if cliente is not None else _Cliente()
    monkeypatch.setattr(module, "get_bigquery_client", lambda: cliente)

    async def _sem_redis():
        return None

    monkeypatch.setattr(module, "get_async_redis_client", _sem_redis)
    return module, cliente


async def _ler(module, **kwargs):
    kwargs.setdefault("query", "select 1")
    kwargs.setdefault("cache_ttl_seconds", 0)
    return await module.get_bigquery_result(**kwargs)


# ---------------------------------------------------------------------------
# Timeout: os dois relógios, um tipo só
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_interno_do_result_vira_timeout_error(monkeypatch):
    """O ramo que a suíte nunca exercitou.

    Antes chegava como `Exception("Failed to execute BigQuery query: ...")` —
    indistinguível de um bug de código para quem trata a falha.
    """
    job = _Job(erro=concurrent.futures.TimeoutError("job did not complete in time"))
    module, _cliente = _montar(monkeypatch, "bq_exc_timeout_interno", _Cliente(job))

    with pytest.raises(TimeoutError) as exc:
        await _ler(module, timeout_seconds=5)

    assert "timed out after 5s" in str(exc.value)


@pytest.mark.asyncio
async def test_timeout_interno_e_externo_dao_o_mesmo_tipo(monkeypatch):
    """Qual relógio dispara primeiro é corrida; o contrato não pode depender disso.

    Um módulo só, dois clientes: carregar o módulo duas vezes daria duas
    classes `BigQueryTimeoutError` distintas e o `is` compararia o dublê de
    teste, não o comportamento.
    """
    module, _cliente = _montar(monkeypatch, "bq_exc_dois_relogios")

    # Relógio de dentro: quem estoura é o `.result(timeout=...)`, na thread.
    job_estourado = _Job(
        erro=concurrent.futures.TimeoutError("job did not complete in time")
    )
    monkeypatch.setattr(module, "get_bigquery_client", lambda: _Cliente(job_estourado))
    with pytest.raises(TimeoutError) as interno:
        await _ler(module, timeout_seconds=5)

    # Relógio de fora: o `.result()` nem responde, quem estoura é o `wait_for`.
    class _JobLento(_Job):
        def result(self, page_size=None, timeout=None):
            import time

            time.sleep(2.0)
            return []

    monkeypatch.setattr(module, "get_bigquery_client", lambda: _Cliente(_JobLento()))
    with pytest.raises(TimeoutError) as externo:
        await _ler(module, timeout_seconds=0.2)

    assert type(interno.value) is type(externo.value) is module.BigQueryTimeoutError
    # Mesma frase, só o prazo muda: dá para casar em log sem depender do ramo.
    assert "timed out after 5s" in str(interno.value)
    assert "timed out after 0.2s" in str(externo.value)


@pytest.mark.asyncio
async def test_timeout_interno_preserva_a_causa_original(monkeypatch):
    """`from e`: o traceback do cliente do BigQuery continua alcançável."""
    original = concurrent.futures.TimeoutError("job did not complete in time")
    module, _cliente = _montar(
        monkeypatch, "bq_exc_timeout_causa", _Cliente(_Job(erro=original))
    )

    with pytest.raises(TimeoutError) as exc:
        await _ler(module, timeout_seconds=5)

    causas = []
    atual = exc.value
    while atual is not None:
        causas.append(atual)
        atual = atual.__cause__
    assert original in causas


@pytest.mark.asyncio
async def test_job_abandonado_e_cancelado(monkeypatch):
    """Sem cancelar, o job segue varrendo bytes (e sendo cobrado) sem leitor."""
    job = _Job(erro=concurrent.futures.TimeoutError("boom"))
    module, _cliente = _montar(monkeypatch, "bq_exc_cancela", _Cliente(job))

    with pytest.raises(TimeoutError):
        await _ler(module, timeout_seconds=5)

    assert job.cancelado is True


@pytest.mark.asyncio
async def test_falha_no_cancelamento_nao_mascara_o_timeout(monkeypatch):
    """O cancelamento é best-effort: já estamos no caminho de erro.

    Deixar a falha do `cancel()` subir trocaria a informação útil (estourou o
    prazo) por uma exceção acidental do encerramento.
    """
    job = _Job(
        erro=concurrent.futures.TimeoutError("boom"),
        erro_no_cancel=RuntimeError("cancel falhou"),
    )
    module, _cliente = _montar(monkeypatch, "bq_exc_cancel_falha", _Cliente(job))

    with pytest.raises(TimeoutError):
        await _ler(module, timeout_seconds=5)


@pytest.mark.asyncio
async def test_falha_ao_submeter_o_job_nao_tenta_cancelar(monkeypatch):
    """Não há job para cancelar se `client.query()` nem chegou a devolver um."""
    module, _cliente = _montar(
        monkeypatch,
        "bq_exc_sem_job",
        _Cliente(erro_na_submissao=RuntimeError("conexão recusada")),
    )

    with pytest.raises(module.BigQueryQueryError):
        await _ler(module, timeout_seconds=5)


# ---------------------------------------------------------------------------
# Os outros três desfechos
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_erro_inesperado_vira_bigquery_query_error(monkeypatch):
    module, _cliente = _montar(
        monkeypatch, "bq_exc_inesperado", _Cliente(_Job(erro=RuntimeError("boom")))
    )

    with pytest.raises(module.BigQueryQueryError, match="Failed to execute"):
        await _ler(module)


@pytest.mark.asyncio
async def test_bigquery_query_error_preserva_a_causa(monkeypatch):
    """Sem `from e`, o `raise Exception(str(e))` original jogava fora o traceback."""
    original = RuntimeError("boom")
    module, _cliente = _montar(
        monkeypatch, "bq_exc_causa", _Cliente(_Job(erro=original))
    )

    with pytest.raises(module.BigQueryQueryError) as exc:
        await _ler(module)

    assert exc.value.__cause__ is original


@pytest.mark.asyncio
async def test_bigquery_query_error_continua_sendo_exception(monkeypatch):
    """Contrato preservado: quem já capturava largo não quebra.

    `pluscode_service` captura `Exception` para transformar a falha no dict de
    erro que a tool devolve ao agente.
    """
    module, _cliente = _montar(
        monkeypatch, "bq_exc_subclasse", _Cliente(_Job(erro=RuntimeError("boom")))
    )

    assert issubclass(module.BigQueryQueryError, Exception)
    with pytest.raises(Exception, match="Failed to execute BigQuery query"):
        await _ler(module)


@pytest.mark.asyncio
async def test_hierarquia_mantem_os_tipos_disjuntos(monkeypatch):
    """O escalonamento de `except` em `pluscode_service` depende disto.

    Lá os ramos vão de prazo → `GoogleAPIError` → falha inesperada da leitura →
    `Exception`. A ordem só separa as causas enquanto os três primeiros tipos
    não se contiverem: bastaria `BigQueryTimeoutError` passar a herdar de
    `GoogleAPIError` para o ramo de infraestrutura engolir o de prazo, e a
    regressão seria silenciosa — o tool call continuaria devolvendo um dict,
    só que sempre o mesmo.
    """
    module, _cliente = _montar(monkeypatch, "bq_exc_hierarquia")

    assert issubclass(module.BigQueryTimeoutError, TimeoutError)
    assert issubclass(module.BigQueryQueryError, Exception)
    assert not issubclass(module.BigQueryTimeoutError, GoogleAPIError)
    assert not issubclass(module.BigQueryQueryError, GoogleAPIError)
    assert not issubclass(module.BigQueryQueryError, TimeoutError)
    # E o inverso: uma falha conhecida do Google não pode cair no ramo de prazo.
    assert not issubclass(GoogleAPIError, module.BigQueryTimeoutError)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "erro",
    [
        BadRequest("400 Error while reading table: Spreadsheet not found"),
        Forbidden("403 Permission denied on Drive file"),
    ],
    ids=["planilha-fora-do-ar", "sem-permissao-no-drive"],
)
async def test_google_api_error_sobe_com_o_tipo_original(monkeypatch, erro):
    """É o que permite a `pluscode_service` separar planilha caída de bug nosso.

    Envolver em `BigQueryQueryError` apagaria a distinção e derrubaria o
    fallback de instruções temáticas do CHATR-119.
    """
    module, _cliente = _montar(
        monkeypatch, f"bq_exc_google_{type(erro).__name__}", _Cliente(_Job(erro=erro))
    )

    with pytest.raises(type(erro)):
        await _ler(module)


@pytest.mark.asyncio
async def test_not_found_continua_degradando_para_vazio(monkeypatch):
    """Tabela ausente não é erro para o chamador — é resultado vazio."""
    module, _cliente = _montar(
        monkeypatch, "bq_exc_not_found", _Cliente(_Job(erro=NotFound("sumiu")))
    )

    assert await _ler(module) == []


@pytest.mark.asyncio
async def test_nenhuma_falha_escapa_como_exception_crua(monkeypatch):
    """Varredura do contrato inteiro: todo desfecho tem tipo próprio.

    O ponto do teste é o `type(...) is Exception`: era assim que a falha
    inesperada chegava antes, e é o que não pode voltar a acontecer para
    nenhum dos ramos.
    """
    casos = [
        (concurrent.futures.TimeoutError("prazo"), TimeoutError),
        (RuntimeError("bug"), None),  # BigQueryQueryError, resolvido abaixo
        (BadRequest("400"), BadRequest),
    ]

    for i, (erro, esperado) in enumerate(casos):
        module, _cliente = _montar(
            monkeypatch, f"bq_exc_varredura_{i}", _Cliente(_Job(erro=erro))
        )
        esperado = esperado or module.BigQueryQueryError
        with pytest.raises(esperado) as exc:
            await _ler(module, timeout_seconds=5)
        assert type(exc.value) is not Exception


# ---------------------------------------------------------------------------
# Executor dedicado
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leitura_roda_no_pool_dedicado(monkeypatch):
    """Leitura travada não pode consumir vaga da gravação de log e alerta.

    Antes, o `run_in_executor(None, ...)` colocava as leituras no mesmo pool
    default que `save_response_in_bq_background`,
    `save_feedback_in_bq_background` e `save_cor_alert_in_bq_background` usam —
    e uma leitura que estourou o prazo mantém a thread ocupada até a query
    terminar sozinha.
    """
    job = _Job(linhas=[])
    module, _cliente = _montar(monkeypatch, "bq_exc_pool", _Cliente(job))

    await _ler(module)

    assert job.thread_da_execucao.startswith("bq-read")


@pytest.mark.asyncio
async def test_pool_dedicado_respeita_o_tamanho_configurado(monkeypatch):
    """O limite é intencional: saturação vira TimeoutError, não fila invisível."""
    module, _cliente = _montar(
        monkeypatch, "bq_exc_pool_tamanho", None, **{"BIGQUERY_READ_MAX_WORKERS": 3}
    )

    executor = module._get_read_executor()

    assert executor._max_workers == 3
    # Segunda chamada devolve o mesmo pool: um por processo, como o client.
    assert module._get_read_executor() is executor


@pytest.mark.asyncio
async def test_shutdown_do_pool_e_idempotente(monkeypatch):
    """`atexit` pode disparar junto com um encerramento explícito."""
    module, _cliente = _montar(monkeypatch, "bq_exc_pool_shutdown")

    module._get_read_executor()
    module._shutdown_read_executor()
    module._shutdown_read_executor()  # não pode estourar

    assert module._read_executor is None


@pytest.mark.asyncio
async def test_gravacao_continua_no_executor_default(monkeypatch):
    """O isolamento tem dois lados: a escrita não pode migrar para o pool novo."""
    module, _cliente = _montar(monkeypatch, "bq_exc_pool_escrita")

    threads = []

    def _gravar(*_args, **_kwargs):
        threads.append(threading.current_thread().name)

    monkeypatch.setattr(module, "save_response_in_bq", _gravar)
    await module.save_response_in_bq_background(
        data={"a": 1}, endpoint="/t", dataset_id="d", table_id="t"
    )

    assert threads and not threads[0].startswith("bq-read")


@pytest.mark.asyncio
async def test_pool_saturado_vira_timeout_e_nao_espera_infinita(monkeypatch):
    """Com o pool cheio, a espera por vaga corre dentro do mesmo orçamento."""
    import time

    class _JobLento(_Job):
        def result(self, page_size=None, timeout=None):
            time.sleep(1.5)
            return []

    module, _cliente = _montar(
        monkeypatch,
        "bq_exc_pool_saturado",
        _Cliente(_JobLento()),
        **{"BIGQUERY_READ_MAX_WORKERS": 1},
    )

    inicio = time.monotonic()
    resultados = await asyncio.gather(
        *[_ler(module, query=f"select {i}", timeout_seconds=0.3) for i in range(4)],
        return_exceptions=True,
    )
    duracao = time.monotonic() - inicio

    assert all(isinstance(r, TimeoutError) for r in resultados), resultados
    # Sem o orçamento, os três últimos esperariam a vaga em série (~4,5s).
    assert duracao < 1.5, duracao
