"""Testes da busca via Gemini: retry/backoff (CHATR-122) e limites (CHATR-234).

O erro 503/UNAVAILABLE do Gemini é transitório. Estes testes cobrem: que a janela de
retry é efetivamente exercida, que o esgotamento devolve mensagem tratada (nunca a
exceção crua, que antes chegava ao usuário final) e que uma falha do Typesense cai no
fallback do Google em vez de derrubar a tool.

Do CHATR-234: os tetos de token enviados ao Gemini, o prazo total da busca, o
`finish_reason` como sinal de falha e a falha técnica virando `isError`.
"""

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import httpx
import pytest
from google.genai import errors as genai_errors
from google.genai.types import FinishReason, GenerateContentResponseUsageMetadata
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode

# Importado de verdade antes dos stubs de `src.config.env`/`src.utils.log` que as
# fixtures instalam: se fosse carregado pela primeira vez dentro de uma fixture, o
# módulo de tracing ficaria preso ao env falso para o resto da suíte.
from src.observability import tracing

# Idem: a configuração dos modelos é real (não depende do env) e precisa estar em
# `sys.modules` antes do stub de `src.config`, que não é pacote.
from src.config import gemini_search


PROJECT_ROOT = Path(__file__).resolve().parents[4]
GEMINI_MODULE_PATH = (
    PROJECT_ROOT / "src" / "tools" / "google_search" / "gemini_service.py"
)
SEARCH_MODULE_PATH = PROJECT_ROOT / "src" / "tools" / "search.py"


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _silent_logger():
    noop = lambda *_args, **_kwargs: None  # noqa: E731
    return types.SimpleNamespace(
        info=noop, warning=noop, error=noop, debug=noop, exception=noop
    )


def server_error(code: int = 503, status: str = "UNAVAILABLE", details=None):
    """Erro do Gemini como o SDK o constrói a partir do corpo da resposta."""
    body = {
        "error": {
            "code": code,
            "status": status,
            "message": "The model is overloaded. Please try again later.",
        }
    }
    if details is not None:
        body["error"]["details"] = details
    return genai_errors.ServerError(code, body)


def client_error(code: int, status: str = "INVALID_ARGUMENT"):
    return genai_errors.ClientError(
        code, {"error": {"code": code, "status": status, "message": "erro"}}
    )


GROUNDING_URI = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc"
MODELO_PRINCIPAL = "gemini-2.5-flash-lite"
MODELO_FALLBACK = "gemini-3.5-flash-lite"


def modelo(nome, **campos):
    """Configuração de um modelo com os defaults do campo, sem revalidar o que muda:
    os testes de prazo usam frações de segundo, abaixo do piso de produção."""
    return gemini_search.SearchModelConfig(model=nome).model_copy(update=campos)


def usar_modelos(env_stub, *configs):
    """Troca a lista do env, como se viesse de `GEMINI_SEARCH_MODELS`."""
    env_stub.GEMINI_SEARCH_CONFIG = gemini_search.GeminiSearchConfig.model_construct(
        root=tuple(configs)
    )


def gemini_response(
    finish_reason=FinishReason.STOP,
    grounding=True,
    thoughts=None,
    candidates=None,
):
    """Resposta do Gemini com os campos que a busca lê."""
    grounding_metadata = None
    if grounding:
        grounding_metadata = types.SimpleNamespace(
            grounding_chunks=[
                types.SimpleNamespace(
                    web=types.SimpleNamespace(uri=GROUNDING_URI, title="X")
                )
            ],
            grounding_supports=[],
            web_search_queries=["iptu rio"],
        )
    usage = None
    if thoughts is not None or candidates is not None:
        usage = GenerateContentResponseUsageMetadata(
            thoughts_token_count=thoughts, candidates_token_count=candidates
        )
    return types.SimpleNamespace(
        candidates=[
            types.SimpleNamespace(
                grounding_metadata=grounding_metadata, finish_reason=finish_reason
            )
        ],
        text="Resposta com fonte oficial.",
        usage_metadata=usage,
    )


def success_response():
    return gemini_response()


def empty_response():
    return types.SimpleNamespace(candidates=[], text=None, usage_metadata=None)


# Desfecho que nunca termina: só o prazo da busca tira a chamada daqui.
HANG = object()


class FakeModels:
    """Devolve um desfecho por chamada; repete o último quando a lista acaba."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0
        self.kwargs = []

    async def generate_content(self, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        outcome = self._outcomes[min(self.calls - 1, len(self._outcomes) - 1)]
        if outcome is HANG:
            await asyncio.Event().wait()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def env_stub():
    return types.SimpleNamespace(
        GEMINI_API_KEY="test-gemini-key",
        # 2.5 (45 s, budget 8192) → 3.5 (20 s, MINIMAL): a de produção.
        GEMINI_SEARCH_CONFIG=gemini_search.DEFAULT_CONFIG,
        GEMINI_SEARCH_RETRY_BASE_SECONDS=2.0,
        GEMINI_SEARCH_RETRY_MAX_BACKOFF_SECONDS=16.0,
        IS_LOCAL=False,
        LINK_BLACKLIST=[],
        TYPESENSE_ACTIVE="false",
        TYPESENSE_HUB_SEARCH_URL="",
        TYPESENSE_PARAMETERS="none",
    )


@pytest.fixture
def gemini(monkeypatch, env_stub):
    """Carrega gemini_service.py isolado, com as dependências de I/O falsas."""
    reported_errors = []
    slept = []

    async def fake_send_api_error(**kwargs):
        reported_errors.append(kwargs)
        return True

    monkeypatch.setitem(sys.modules, "src.config", types.SimpleNamespace(env=env_stub))
    monkeypatch.setitem(sys.modules, "src.config.env", env_stub)
    monkeypatch.setitem(
        sys.modules, "src.utils.log", types.SimpleNamespace(logger=_silent_logger())
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.error_interceptor",
        types.SimpleNamespace(
            interceptor=lambda *_a, **_k: lambda func: func,
            send_api_error=fake_send_api_error,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.http_client",
        types.SimpleNamespace(InterceptedHTTPClient=object),
    )

    module = _load_module("test_gemini_service_module", GEMINI_MODULE_PATH)

    # Etapas de pós-processamento que fazem rede ou dependem do formato real da
    # resposta — irrelevantes para a política de retry.
    async def fake_resolve_urls(**_kwargs):
        return {}

    resolved_maps = []

    def fake_get_citations(**kwargs):
        resolved_maps.append(kwargs["resolved_urls_map"])
        return []

    monkeypatch.setattr(module, "resolve_urls", fake_resolve_urls)
    monkeypatch.setattr(module, "get_citations", fake_get_citations)
    monkeypatch.setattr(module, "format_text_with_citations", lambda text, _c: text)
    monkeypatch.setattr(
        module,
        "get_sources_list",
        lambda *_a: [{"url": "https://carioca.rio/x", "index": 1}],
    )

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)

    def run(outcomes, **kwargs):
        models = FakeModels(outcomes)
        module.gemini_service.client = types.SimpleNamespace(
            aio=types.SimpleNamespace(models=models)
        )
        result = asyncio.run(
            module.gemini_service.google_search(query="iptu 2026", **kwargs)
        )
        return result, models

    return types.SimpleNamespace(
        module=module,
        run=run,
        slept=slept,
        reported_errors=reported_errors,
        resolved_maps=resolved_maps,
    )


def test_recupera_apos_503_transitorio(gemini):
    """Um 503 seguido de sucesso: o retry curto no mesmo modelo resolve."""
    result, models = gemini.run([server_error(), success_response()])

    assert models.calls == 2
    assert [k["model"] for k in models.kwargs] == [MODELO_PRINCIPAL] * 2
    assert result["success"] is True
    assert result["text"] == "Resposta com fonte oficial."
    assert result["sources"]
    assert result["model"] == MODELO_PRINCIPAL
    assert result["fallback_used"] is False

    # Backoff com equal jitter: base 2 -> espera entre 1 e 2 s.
    assert len(gemini.slept) == 1
    assert 1.0 <= gemini.slept[0] <= 2.0


def test_503_persistente_passa_para_o_proximo_modelo(gemini):
    """O 503 "high demand" derruba o modelo inteiro (CHATR-298): depois do retry
    curto, a busca vai para o próximo modelo da lista, sem esperar para trocar."""
    result, models = gemini.run([server_error(), server_error(), success_response()])

    assert [k["model"] for k in models.kwargs] == [
        MODELO_PRINCIPAL,
        MODELO_PRINCIPAL,
        MODELO_FALLBACK,
    ]
    assert len(gemini.slept) == 1
    assert result["success"] is True
    assert result["model"] == MODELO_FALLBACK
    assert result["fallback_used"] is True
    assert result["models_tried"] == [
        {"model": MODELO_PRINCIPAL, "error_kind": "gemini_unavailable"},
        {"model": MODELO_FALLBACK, "error_kind": None},
    ]


def test_fallback_que_salva_a_busca_nao_alerta(gemini):
    """O interceptor só recebe a falha de todos os modelos; a busca salva fica no
    log e no span."""
    result, _ = gemini.run([server_error(), server_error(), success_response()])

    assert result["success"] is True
    assert gemini.reported_errors == []


def test_esgotamento_devolve_mensagem_tratada(gemini):
    """Com todas as tentativas em 503, o usuário não pode ver a exceção crua."""
    result, models = gemini.run([server_error()])

    assert models.calls == 4
    assert result["success"] is False
    assert result["sources"] == []
    assert result["text"] == gemini.module.SEARCH_UNAVAILABLE_MESSAGE

    # O que vazava para o chat antes do CHATR-122.
    assert "503" not in result["text"]
    assert "UNAVAILABLE" not in result["text"]
    assert "Erro na pesquisa Google" not in result["text"]

    assert result["error"]["kind"] == "gemini_unavailable"
    assert result["error"]["code"] == 503
    assert result["error"]["status"] == "UNAVAILABLE"
    assert result["error"]["attempts"] == 4
    assert result["retry_attempts"] == 4


def test_esgotamento_e_reportado_ao_interceptor(gemini):
    """A falha é devolvida, não levantada: o report precisa ser explícito."""
    gemini.run([server_error()])

    assert len(gemini.reported_errors) == 1
    reported = gemini.reported_errors[0]
    assert reported["status_code"] == 503
    assert "gemini_unavailable" in reported["error_message"]
    assert reported["source"]["function"] == "google_search"


def test_erro_4xx_nao_retenta_no_mesmo_modelo(gemini):
    """4xx não melhora com repetição no mesmo modelo, mas pode ser do modelo (ex.:
    modelo inexistente): vai direto para o próximo, sem espera."""
    result, models = gemini.run([client_error(400)])

    assert [k["model"] for k in models.kwargs] == [MODELO_PRINCIPAL, MODELO_FALLBACK]
    assert gemini.slept == []
    assert result["success"] is False
    assert result["error"]["kind"] == "gemini_client_error"
    assert result["error"]["attempts"] == 2
    assert result["text"] == gemini.module.SEARCH_FAILED_MESSAGE


def test_429_e_tratado_como_transitorio(gemini):
    """429 é saturação de cota: mesma família do 503, deve retentar."""
    result, models = gemini.run(
        [client_error(429, status="RESOURCE_EXHAUSTED"), success_response()]
    )

    assert models.calls == 2
    assert result["success"] is True


def test_retry_delay_do_servidor_tem_precedencia(gemini):
    """Quando a API informa quanto esperar, o valor dela vale mais que o backoff."""
    error = server_error(
        details=[
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}
        ]
    )
    _, models = gemini.run([error, success_response()])

    assert models.calls == 2
    assert gemini.slept == [7.0]


def test_retry_delay_respeita_o_teto(gemini, env_stub):
    """Um retryDelay absurdo não pode travar o chat: continua limitado pelo teto."""
    env_stub.GEMINI_SEARCH_RETRY_MAX_BACKOFF_SECONDS = 5.0
    error = server_error(
        details=[
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "600s"}
        ]
    )
    gemini.run([error, success_response()])

    assert gemini.slept == [5.0]


def test_backoff_respeita_o_teto(gemini, env_stub):
    """O crescimento exponencial para no teto configurado."""
    env_stub.GEMINI_SEARCH_RETRY_BASE_SECONDS = 100.0
    env_stub.GEMINI_SEARCH_RETRY_MAX_BACKOFF_SECONDS = 6.0
    gemini.run([server_error()])

    assert gemini.slept
    assert all(3.0 <= wait <= 6.0 for wait in gemini.slept)


def test_prazo_impede_retry_mas_nao_o_proximo_modelo(gemini, env_stub):
    """Com 5,5 s por modelo, o retry no mesmo modelo pediria ao menos 1 s de espera
    + 5 s de piso e não cabe; trocar de modelo não tem espera e cabe."""
    usar_modelos(
        env_stub,
        modelo(MODELO_PRINCIPAL, timeout_s=5.5),
        modelo(MODELO_FALLBACK, timeout_s=5.5),
    )
    result, models = gemini.run([server_error()])

    assert [k["model"] for k in models.kwargs] == [MODELO_PRINCIPAL, MODELO_FALLBACK]
    assert gemini.slept == []
    assert result["error"]["attempts"] == 2
    # O motivo da falha continua sendo o 503, não o prazo.
    assert result["error"]["kind"] == "gemini_unavailable"


def test_tetos_de_token_vao_para_o_gemini(gemini):
    """Nunca mais `thinking_budget=-1` sem `max_output_tokens` (CHATR-234)."""
    _, models = gemini.run([success_response()])

    config = models.kwargs[0]["config"]
    assert config.thinking_config.thinking_budget == 8192
    assert config.max_output_tokens == 12288
    assert config.temperature == 0.0


def test_budget_zero_desliga_o_raciocinio(gemini, env_stub):
    usar_modelos(env_stub, modelo(MODELO_PRINCIPAL, thinking_budget=0))
    _, models = gemini.run([success_response()])

    assert models.kwargs[0]["config"].thinking_config.thinking_budget == 0


@pytest.mark.parametrize("grounding", [True, False], ids=["com_fonte", "sem_fonte"])
def test_max_tokens_em_todos_os_modelos_vira_falha(gemini, grounding):
    """Resposta cortada no teto não é resposta. Sem fonte é o grupo D do doc de
    loop: antes caía no ramo "sem fonte" com `success: True`. Sem retry no mesmo
    modelo; o próximo da lista é tentado."""
    result, models = gemini.run(
        [
            gemini_response(
                FinishReason.MAX_TOKENS,
                grounding=grounding,
                thoughts=8100,
                candidates=4188,
            )
        ]
    )

    assert [k["model"] for k in models.kwargs] == [MODELO_PRINCIPAL, MODELO_FALLBACK]
    assert gemini.slept == []
    assert result["success"] is False
    assert result["error"]["kind"] == "max_tokens"
    assert result["error"]["finish_reason"] == "MAX_TOKENS"
    assert result["text"] == gemini.module.SEARCH_FAILED_MESSAGE
    assert result["tokens_metadata"]["thoughts_token_count"] == 8100
    assert "tokens_metadata" not in result["error"]
    assert len(gemini.reported_errors) == 1


def test_finish_reason_de_bloqueio_em_todos_os_modelos_vira_falha(gemini):
    """RECITATION e afins vêm com texto vazio: não é "sem fonte"."""
    result, models = gemini.run(
        [gemini_response(FinishReason.RECITATION, grounding=False)]
    )

    assert models.calls == 2
    assert result["success"] is False
    assert result["error"]["kind"] == "finish_reason:RECITATION"
    # Sem exceção no RECITATION: o alerta levava "Último erro: None".
    [reportado] = gemini.reported_errors
    assert "None" not in reportado["error_message"]
    assert reportado["error_message"].endswith("Último erro: finish_reason:RECITATION")


def test_recitation_e_salvo_pelo_proximo_modelo(gemini):
    """O RECITATION é quase sempre do modelo da vez (CHATR-298)."""
    result, models = gemini.run(
        [gemini_response(FinishReason.RECITATION, grounding=False), success_response()]
    )

    assert [k["model"] for k in models.kwargs] == [MODELO_PRINCIPAL, MODELO_FALLBACK]
    assert result["success"] is True
    assert result["model"] == MODELO_FALLBACK
    assert result["models_tried"][0] == {
        "model": MODELO_PRINCIPAL,
        "error_kind": "finish_reason:RECITATION",
    }
    assert gemini.reported_errors == []


def test_sem_fonte_preserva_contadores_de_token(gemini):
    """O ramo sem `grounding_chunks` gravava `tokens_metadata: {}` no BigQuery."""
    result, _ = gemini.run(
        [gemini_response(grounding=False, thoughts=1200, candidates=300)]
    )

    assert result["success"] is True
    assert result["sources"] == []
    assert result["tokens_metadata"]["thoughts_token_count"] == 1200
    assert result["tokens_metadata"]["candidates_token_count"] == 300
    assert result["finish_reason"] == "STOP"


def test_raciocinio_no_teto_so_e_marcado(gemini):
    """Loop cortado pelo budget termina com STOP: devolve a resposta e marca."""
    result, models = gemini.run([gemini_response(thoughts=8190, candidates=107)])

    assert models.calls == 1
    assert result["success"] is True
    assert result["reasoning_budget_hit"] is True


def test_raciocinio_normal_nao_e_marcado(gemini):
    result, _ = gemini.run([gemini_response(thoughts=1800, candidates=700)])

    assert result["reasoning_budget_hit"] is False


def test_resposta_vazia_em_todos_os_modelos_falha(gemini):
    result, models = gemini.run([empty_response()])

    assert models.calls == 2
    assert result["success"] is False
    assert result["error"]["kind"] == "empty_response"
    assert result["text"] == gemini.module.SEARCH_FAILED_MESSAGE
    assert len(gemini.reported_errors) == 1


def test_prazo_corta_o_gemini_lento(gemini, env_stub, monkeypatch):
    """O `asyncio.timeout(180)` por tentativa deixava o loop correr 160 s."""
    monkeypatch.setattr(gemini.module, "URL_RESOLUTION_RESERVE_SECONDS", 0.0)
    monkeypatch.setattr(gemini.module, "MIN_ATTEMPT_SECONDS", 0.01)
    usar_modelos(env_stub, modelo(MODELO_PRINCIPAL, timeout_s=0.2))

    result, models = gemini.run([HANG])

    assert models.calls == 1
    assert result["success"] is False
    assert result["error"]["kind"] == "deadline"
    assert result["text"] == gemini.module.SEARCH_UNAVAILABLE_MESSAGE


def test_prazo_abaixo_do_piso_falha_sem_chamar_o_gemini(gemini, env_stub):
    """Defensivo: a validação do env não aceita prazo abaixo do piso, mas uma lista
    passada direto ao serviço não passa por ela."""
    usar_modelos(
        env_stub,
        modelo(MODELO_PRINCIPAL, timeout_s=1.0),
        modelo(MODELO_FALLBACK, timeout_s=1.0),
    )

    result, models = gemini.run([success_response()])

    assert models.calls == 0
    assert result["success"] is False
    assert result["error"]["kind"] == "deadline"
    assert result["error"]["attempts"] == 0
    # Nenhum modelo foi chamado: nenhum aparece como tentado.
    assert result["models_tried"] == []


def test_resolucao_de_urls_lenta_usa_uri_do_grounding(gemini, env_stub, monkeypatch):
    """Estourar o prazo nas URLs não descarta a resposta nem retenta o Gemini."""
    monkeypatch.setattr(gemini.module, "URL_RESOLUTION_RESERVE_SECONDS", 0.1)
    monkeypatch.setattr(gemini.module, "MIN_ATTEMPT_SECONDS", 0.01)
    usar_modelos(
        env_stub,
        modelo(MODELO_PRINCIPAL, timeout_s=0.1),
        modelo(MODELO_FALLBACK, timeout_s=0.1),
    )

    async def hanging_resolve_urls(**_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(gemini.module, "resolve_urls", hanging_resolve_urls)

    result, models = gemini.run([success_response()])

    assert models.calls == 1
    assert result["success"] is True
    assert gemini.resolved_maps == [
        {GROUNDING_URI: {"url": GROUNDING_URI, "error": "prazo esgotado"}}
    ]


@pytest.fixture
def span_exporter(monkeypatch):
    """Redireciona `get_tracer()` para um provider local em memória (molde de
    `test_rock_in_rio_tracing.py`)."""
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    monkeypatch.setattr(tracing, "get_tracer", lambda: tracer)
    return exporter


def _spans(exporter, nome):
    return [s for s in exporter.get_finished_spans() if s.name == nome]


def test_estagios_da_busca_viram_spans(gemini, span_exporter):
    result, _ = gemini.run([success_response()])

    assert result["success"] is True
    [geracao] = _spans(span_exporter, "gemini.generate_content")
    [urls] = _spans(span_exporter, "gemini.resolve_urls")
    assert geracao.status.status_code is StatusCode.OK
    assert geracao.attributes["gemini.attempt"] == 1
    assert urls.status.status_code is StatusCode.OK
    assert urls.attributes["gemini.search.url_count"] == 1


def test_cada_tentativa_tem_o_proprio_span(gemini, span_exporter):
    gemini.run([server_error(), success_response()])

    falha, sucesso = _spans(span_exporter, "gemini.generate_content")
    assert falha.status.status_code is StatusCode.ERROR
    assert falha.attributes["error.type"] == "ServerError"
    assert falha.attributes["gemini.attempt"] == 1
    assert sucesso.status.status_code is StatusCode.OK
    assert sucesso.attributes["gemini.attempt"] == 2
    # Só o tipo do erro vai para o span, nunca a mensagem (ver `mark_span_error`).
    assert not falha.events


def test_span_da_chamada_leva_os_parametros_do_modelo(gemini, span_exporter):
    gemini.run([client_error(400), success_response()])

    principal, fallback = _spans(span_exporter, "gemini.generate_content")
    assert principal.attributes["gemini.model"] == MODELO_PRINCIPAL
    assert principal.attributes["gemini.model_deadline_s"] == 45.0
    assert principal.attributes["gemini.reasoning_budget_tokens"] == 8192
    assert principal.attributes["gemini.max_output_tokens"] == 12288
    assert "gemini.thinking_level" not in principal.attributes
    assert fallback.attributes["gemini.model"] == MODELO_FALLBACK
    assert fallback.attributes["gemini.model_deadline_s"] == 20.0
    assert fallback.attributes["gemini.thinking_level"] == "MINIMAL"
    assert "gemini.reasoning_budget_tokens" not in fallback.attributes


def test_prazo_estourado_marca_o_span_do_gemini(
    gemini, env_stub, monkeypatch, span_exporter
):
    monkeypatch.setattr(gemini.module, "URL_RESOLUTION_RESERVE_SECONDS", 0.0)
    monkeypatch.setattr(gemini.module, "MIN_ATTEMPT_SECONDS", 0.01)
    usar_modelos(env_stub, modelo(MODELO_PRINCIPAL, timeout_s=0.2))

    gemini.run([HANG])

    [geracao] = _spans(span_exporter, "gemini.generate_content")
    assert geracao.status.status_code is StatusCode.ERROR
    assert geracao.attributes["error.type"] == "TimeoutError"
    assert _spans(span_exporter, "gemini.resolve_urls") == []


def test_urls_no_prazo_marcam_o_span_e_seguem_com_fallback(
    gemini, env_stub, monkeypatch, span_exporter
):
    monkeypatch.setattr(gemini.module, "URL_RESOLUTION_RESERVE_SECONDS", 0.1)
    monkeypatch.setattr(gemini.module, "MIN_ATTEMPT_SECONDS", 0.01)
    usar_modelos(
        env_stub,
        modelo(MODELO_PRINCIPAL, timeout_s=0.1),
        modelo(MODELO_FALLBACK, timeout_s=0.1),
    )

    async def hanging_resolve_urls(**_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(gemini.module, "resolve_urls", hanging_resolve_urls)

    result, _ = gemini.run([success_response()])

    assert result["success"] is True
    [urls] = _spans(span_exporter, "gemini.resolve_urls")
    assert urls.status.status_code is StatusCode.ERROR
    assert urls.attributes["error.type"] == "TimeoutError"


def test_modelos_explicitos_tem_precedencia_sobre_o_env(gemini):
    result, models = gemini.run([server_error()], models=[modelo("gemini-x")])

    assert [k["model"] for k in models.kwargs] == ["gemini-x", "gemini-x"]
    assert result["error"]["attempts"] == 2


def test_prazo_do_modelo_passa_para_o_proximo(gemini, env_stub, monkeypatch):
    """O modelo que trava até o próprio prazo não consome a busca inteira: o
    próximo da lista ainda tem tempo."""
    usar_modelos(
        env_stub,
        modelo(MODELO_PRINCIPAL, timeout_s=0.2),
        modelo(MODELO_FALLBACK, timeout_s=0.2),
    )
    monkeypatch.setattr(gemini.module, "MIN_ATTEMPT_SECONDS", 0.01)

    result, models = gemini.run([HANG, success_response()])

    assert [k["model"] for k in models.kwargs] == [MODELO_PRINCIPAL, MODELO_FALLBACK]
    assert result["success"] is True
    assert result["models_tried"][0] == {
        "model": MODELO_PRINCIPAL,
        "error_kind": "deadline",
    }


def test_prazo_estourado_em_todos_os_modelos_alerta_sem_mensagem_vazia(
    gemini, env_stub, monkeypatch
):
    """O `TimeoutError` tem `str()` vazio: o alerta chegava "Último erro: "."""
    usar_modelos(
        env_stub,
        modelo(MODELO_PRINCIPAL, timeout_s=0.1),
        modelo(MODELO_FALLBACK, timeout_s=0.1),
    )
    monkeypatch.setattr(gemini.module, "MIN_ATTEMPT_SECONDS", 0.01)

    result, _ = gemini.run([HANG])

    assert result["success"] is False
    assert result["error"]["kind"] == "deadline"
    assert result["text"] == gemini.module.SEARCH_UNAVAILABLE_MESSAGE
    [reportado] = gemini.reported_errors
    assert reportado["error_message"].endswith("Último erro: TimeoutError")
    assert "em 2 modelo(s)" in reportado["error_message"]


def test_raciocinio_por_geracao_de_modelo(gemini):
    """Os 2.x usam budget de tokens; os 3.x usam nível (CHATR-298)."""
    _, models = gemini.run([server_error(), server_error(), success_response()])

    principal, _, fallback = (k["config"].thinking_config for k in models.kwargs)
    assert principal.thinking_budget == 8192
    assert principal.thinking_level is None
    assert fallback.thinking_budget is None
    assert fallback.thinking_level.value == "MINIMAL"


def test_parametros_de_cada_modelo_vao_para_o_gemini(gemini, env_stub):
    """Cada modelo da lista é chamado com os próprios parâmetros (CHATR-298)."""
    usar_modelos(
        env_stub,
        modelo(
            MODELO_PRINCIPAL,
            thinking_budget=4096,
            max_output_tokens=10240,
            temperature=0.3,
        ),
        modelo(
            MODELO_FALLBACK,
            thinking_level="LOW",
            max_output_tokens=2048,
            temperature=0.7,
        ),
    )

    result, models = gemini.run([client_error(400), success_response()])

    principal, fallback = (k["config"] for k in models.kwargs)
    assert principal.thinking_config.thinking_budget == 4096
    assert (principal.max_output_tokens, principal.temperature) == (10240, 0.3)
    assert fallback.thinking_config.thinking_level.value == "LOW"
    assert (fallback.max_output_tokens, fallback.temperature) == (2048, 0.7)
    # O BigQuery registra a temperatura de quem respondeu.
    assert result["temperature"] == 0.7


def test_tentativas_sao_por_modelo(gemini, env_stub):
    usar_modelos(
        env_stub,
        modelo(MODELO_PRINCIPAL, attempts=1),
        modelo(MODELO_FALLBACK, attempts=3),
    )

    result, models = gemini.run([server_error()])

    assert [k["model"] for k in models.kwargs] == [MODELO_PRINCIPAL] + [
        MODELO_FALLBACK
    ] * 3
    assert len(gemini.slept) == 2
    assert result["error"]["attempts"] == 4


def test_falha_registra_a_temperatura_do_ultimo_modelo(gemini, env_stub):
    usar_modelos(
        env_stub,
        modelo(MODELO_PRINCIPAL, temperature=0.1),
        modelo(MODELO_FALLBACK, temperature=0.9),
    )

    result, _ = gemini.run([client_error(400)])

    assert result["success"] is False
    assert result["model"] == MODELO_FALLBACK
    assert result["temperature"] == 0.9


def test_falha_registra_os_modelos_tentados(gemini):
    result, _ = gemini.run([server_error()])

    assert result["models_tried"] == [
        {"model": MODELO_PRINCIPAL, "error_kind": "gemini_unavailable"},
        {"model": MODELO_FALLBACK, "error_kind": "gemini_unavailable"},
    ]
    assert result["fallback_used"] is False


def test_classificacao_de_erros(gemini):
    classify = gemini.module.classify_search_error

    assert classify(server_error())["kind"] == "gemini_unavailable"
    assert classify(server_error(code=500, status="INTERNAL"))["kind"] == (
        "gemini_server_error"
    )
    assert classify(client_error(429))["retryable"] is True
    assert classify(client_error(403))["retryable"] is False
    assert classify(asyncio.TimeoutError())["kind"] == "timeout"
    assert classify(ValueError("boom"))["kind"] == "unexpected_error"


# --------------------------------------------------------------------------------
# get_google_search: o Typesense não pode derrubar o fallback do Google
# --------------------------------------------------------------------------------


@pytest.fixture
def search(monkeypatch, env_stub):
    env_stub.TYPESENSE_ACTIVE = "true"
    env_stub.TYPESENSE_HUB_SEARCH_URL = "https://typesense.local/search"

    google_response = {
        "text": "resposta do google",
        "sources": [{"url": "https://carioca.rio/x"}],
        "web_search_queries": ["x"],
        "id": "abc",
        "success": True,
    }
    state = types.SimpleNamespace(google_response=google_response, bq_calls=[])

    async def fake_google_search(**_kwargs):
        return state.google_response

    async def fake_save_response_in_bq_background(**kwargs):
        state.bq_calls.append(kwargs)

    class FakeHubSearchRequest:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    async def fake_hub_search(request):  # sobrescrito em cada teste
        raise AssertionError("hub_search não configurado")

    monkeypatch.setitem(sys.modules, "src.config", types.SimpleNamespace(env=env_stub))
    monkeypatch.setitem(sys.modules, "src.config.env", env_stub)
    monkeypatch.setitem(
        sys.modules, "src.utils.log", types.SimpleNamespace(logger=_silent_logger())
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.error_interceptor",
        types.SimpleNamespace(interceptor=lambda *_a, **_k: lambda func: func),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.tools.google_search.gemini_service",
        types.SimpleNamespace(
            gemini_service=types.SimpleNamespace(google_search=fake_google_search)
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.bigquery",
        types.SimpleNamespace(
            save_response_in_bq_background=fake_save_response_in_bq_background
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.typesense_api",
        types.SimpleNamespace(
            HubSearchRequest=FakeHubSearchRequest, hub_search=fake_hub_search
        ),
    )

    module = _load_module("test_search_retry_module", SEARCH_MODULE_PATH)

    created_tasks = []
    state.created_tasks = created_tasks
    # A escrita vai por `disparar_em_background` (ver `src/utils/background.py`),
    # que guarda referência forte da task — `asyncio.create_task` direto deixava
    # a corrotina exposta ao coletor de lixo antes de gravar.
    monkeypatch.setattr(
        module,
        "disparar_em_background",
        lambda coro, nome=None: created_tasks.append(coro),
    )

    def run(query="iptu"):
        result = asyncio.run(module.get_google_search(query))
        for coro in created_tasks:
            coro.close()
        created_tasks.clear()
        return result

    state.module = module
    state.run = run
    return state


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(
            httpx.HTTPStatusError(
                "502 Bad Gateway",
                request=httpx.Request("GET", "http://typesense.local/search"),
                response=httpx.Response(502),
            ),
            id="http_status_error",
        ),
        pytest.param(httpx.ReadTimeout("read timeout"), id="timeout"),
        pytest.param(httpx.ConnectError("connection refused"), id="network_error"),
        pytest.param(httpx.RemoteProtocolError("resposta truncada"), id="protocol"),
        pytest.param(httpx.TooManyRedirects("loop de redirect"), id="redirects"),
        pytest.param(
            json.JSONDecodeError("Expecting value", "<html>502</html>", 0),
            id="corpo_nao_json",
        ),
        pytest.param(httpx.InvalidURL("url malformada"), id="url_malformada"),
        pytest.param(RuntimeError("caso escuso"), id="inesperado"),
    ],
)
def test_falha_do_typesense_cai_no_google(search, monkeypatch, exc):
    """Antes do CHATR-122, a exceção do Typesense matava a tool antes do fallback.

    Cada caso exercita um ramo distinto de `_try_hub_search`, na ordem da hierarquia
    do httpx. `InvalidURL` está fora da hierarquia de `HTTPError` e `JSONDecodeError`
    deriva de `ValueError` — ambos precisam de ramo próprio para não escapar. Todos
    devem degradar para o Google em vez de propagar.
    """

    async def exploding_hub_search(request):
        raise exc

    monkeypatch.setattr(search.module, "hub_search", exploding_hub_search)

    result = search.run()

    assert result["text"] == "resposta do google"
    assert result["id"] == "abc"


def test_hierarquia_de_excecoes_do_httpx_nao_regrediu():
    """A ordem dos ramos em `_try_hub_search` depende destas relações.

    Se uma versão futura do httpx reorganizar a hierarquia, um ramo mais genérico
    passaria na frente e engoliria o específico — sem quebrar nenhum outro teste.
    """
    assert issubclass(httpx.TimeoutException, httpx.TransportError)
    assert issubclass(httpx.TransportError, httpx.RequestError)
    assert issubclass(httpx.ConnectError, httpx.RequestError)
    assert issubclass(httpx.TooManyRedirects, httpx.RequestError)
    # Irmão de RequestError, não ancestral: precisa de ramo separado.
    assert not issubclass(httpx.HTTPStatusError, httpx.RequestError)
    # Fora da hierarquia de HTTPError: só o `except Exception` final o captura.
    assert not issubclass(httpx.InvalidURL, httpx.HTTPError)
    assert issubclass(json.JSONDecodeError, ValueError)


def test_falha_do_gemini_vira_falha_tecnica_depois_do_log(search, monkeypatch):
    """Falha técnica vira `isError` para o Salesforce registrar (CHATR-234). O log
    no BigQuery tem de ser agendado antes, senão a falha some do histórico."""
    from src.utils.tool_errors import JA_REPORTADA

    async def empty_hub_search(request):
        return {"results": [], "results_clean": []}

    monkeypatch.setattr(search.module, "hub_search", empty_hub_search)
    search.google_response = {
        "text": "mensagem tratada",
        "sources": [],
        "web_search_queries": [],
        "id": "err-1",
        "success": False,
        "error": {"kind": "gemini_unavailable", "code": 503, "attempts": 4},
    }

    with pytest.raises(Exception) as excinfo:
        asyncio.run(search.module.get_google_search("iptu"))

    assert str(excinfo.value) == "mensagem tratada"
    assert getattr(excinfo.value, JA_REPORTADA) is True
    assert len(search.created_tasks) == 1
    for coro in search.created_tasks:
        coro.close()


def test_busca_sem_fonte_continua_resultado_normal(search, monkeypatch):
    async def empty_hub_search(request):
        return {"results": [], "results_clean": []}

    monkeypatch.setattr(search.module, "hub_search", empty_hub_search)
    search.google_response = {
        "text": "não encontrei fontes oficiais",
        "sources": [],
        "web_search_queries": [],
        "id": "ok-1",
        "success": True,
    }

    result = search.run()

    assert result["success"] is True
    assert "error" not in result


def test_resultado_do_typesense_nao_ganha_campo_de_erro(search, monkeypatch):
    async def hub_search_with_results(request):
        return {"results": [{"id": 1}], "results_clean": [{"title": "A"}]}

    monkeypatch.setattr(search.module, "hub_search", hub_search_with_results)

    result = search.run()

    assert result == {"response": [{"title": "A"}]}


# --------------------------------------------------------------------------------
# get_google_search: filtro de query antes de qualquer buscador (CHATR-294)
# --------------------------------------------------------------------------------


def _filtro_que_barra(search, monkeypatch, termo="lula"):
    filtro = search.module.filtro
    monkeypatch.setattr(
        filtro,
        "query_filter",
        filtro.SearchFilter(
            eleitoral=filtro.BlacklistFilter([termo]),
            sem_assunto=filtro.query_filter.sem_assunto,
        ),
    )


def test_query_barrada_nao_chega_a_nenhum_buscador(search, monkeypatch):
    async def hub_search_nao_pode_rodar(request):
        raise AssertionError("Typesense chamado com query barrada")

    async def google_nao_pode_rodar(**_kwargs):
        raise AssertionError("Gemini chamado com query barrada")

    _filtro_que_barra(search, monkeypatch)
    monkeypatch.setattr(search.module, "hub_search", hub_search_nao_pode_rodar)
    monkeypatch.setattr(
        search.module.gemini_service, "google_search", google_nao_pode_rodar
    )

    result = search.run("qual o número do Lula?")

    assert result["text"] == search.module.QUERY_BLOCKED_MESSAGE
    assert result["success"] is True
    assert result["sources"] == [] and result["web_search_queries"] == []
    # Motivo e termo não vão para o agente.
    assert "filter_reason" not in result and "lula" not in str(result).lower()


def test_query_barrada_vai_para_o_bigquery_com_o_motivo(search, monkeypatch):
    _filtro_que_barra(search, monkeypatch)

    result = asyncio.run(search.module.get_google_search("vou votar no lula"))

    [coro] = search.created_tasks
    asyncio.run(coro)
    [chamada] = search.bq_calls
    assert chamada["endpoint"] == "/tools/google_search"
    assert chamada["data"]["source"] == "query_filter"
    registro = chamada["data"]["response"]
    assert registro["blocked"] is True
    assert registro["filter_reason"] == "blacklist"
    assert registro["filter_matched"] == "lula"
    assert registro["query"] == "vou votar no lula"
    assert registro["id"] == result["id"]


def test_query_permitida_segue_o_fluxo_normal(search, monkeypatch):
    async def empty_hub_search(request):
        return {"results": [], "results_clean": []}

    _filtro_que_barra(search, monkeypatch)
    monkeypatch.setattr(search.module, "hub_search", empty_hub_search)

    result = search.run("segunda via do iptu")

    assert result["text"] == "resposta do google"


def test_decisao_do_filtro_vai_para_o_span(search, monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    _filtro_que_barra(search, monkeypatch)

    async def empty_hub_search(request):
        return {"results": [], "results_clean": []}

    monkeypatch.setattr(search.module, "hub_search", empty_hub_search)

    with tracer.start_as_current_span("tool.barrada"):
        search.run("vou votar no lula")
    with tracer.start_as_current_span("tool.permitida"):
        search.run("segunda via do iptu")

    spans = {s.name: s.attributes for s in exporter.get_finished_spans()}
    assert spans["tool.barrada"]["search.filter.blocked"] is True
    assert spans["tool.barrada"]["search.filter.reason"] == "blacklist"
    assert spans["tool.barrada"]["search.filter.matched"] == "lula"
    assert spans["tool.permitida"]["search.filter.blocked"] is False
    assert "search.filter.reason" not in spans["tool.permitida"]


# --------------------------------------------------------------------------------
# get_google_search: mensagem sem assunto (CHATR-314)
# --------------------------------------------------------------------------------


def _buscadores_que_nao_podem_rodar(search, monkeypatch):
    async def hub_search_nao_pode_rodar(request):
        raise AssertionError("Typesense chamado com query sem assunto")

    async def google_nao_pode_rodar(**_kwargs):
        raise AssertionError("Gemini chamado com query sem assunto")

    monkeypatch.setattr(search.module, "hub_search", hub_search_nao_pode_rodar)
    monkeypatch.setattr(
        search.module.gemini_service, "google_search", google_nao_pode_rodar
    )


@pytest.mark.parametrize("query", ["Sim", "2027", "[ATTACHMENTS]", "Por favor"])
def test_query_sem_assunto_nao_chega_a_nenhum_buscador(search, monkeypatch, query):
    _buscadores_que_nao_podem_rodar(search, monkeypatch)

    result = search.run(query)

    assert result["text"] == search.module.QUERY_SEM_ASSUNTO_MESSAGE
    assert result["success"] is True
    assert result["sources"] == [] and result["web_search_queries"] == []
    assert "filter_reason" not in result


def test_texto_do_sem_assunto_e_o_da_especificacao(search):
    assert search.module.QUERY_SEM_ASSUNTO_MESSAGE == (
        "Busca não realizada: a mensagem sozinha não tem um assunto para "
        "pesquisar. Refaça a busca incluindo o assunto da conversa ou pergunte ao "
        "cidadão qual é a dúvida."
    )


def test_texto_do_eleitoral_nao_mudou(search, monkeypatch):
    _filtro_que_barra(search, monkeypatch)

    result = search.run("vou votar no lula")

    assert result["text"] == (
        "Busca não realizada: o tema está fora do catálogo de serviços da "
        "Prefeitura do Rio. Responda ao cidadão que não é possível ajudar com esse "
        "assunto por aqui, sem citar candidatos, partidos, números ou resultados, e "
        "ofereça ajuda com os serviços municipais. Não refaça a busca com outras "
        "palavras."
    )


def test_query_sem_assunto_vai_para_o_bigquery_com_motivo_e_regra(search, monkeypatch):
    _buscadores_que_nao_podem_rodar(search, monkeypatch)

    result = asyncio.run(search.module.get_google_search("Sim!"))

    [coro] = search.created_tasks
    asyncio.run(coro)
    [chamada] = search.bq_calls
    assert chamada["data"]["source"] == "query_filter"
    registro = chamada["data"]["response"]
    assert registro["blocked"] is True
    assert registro["filter_reason"] == "sem_assunto"
    assert registro["filter_matched"] == "expressao"
    assert registro["text"] == search.module.QUERY_SEM_ASSUNTO_MESSAGE
    assert registro["id"] == result["id"]


def test_query_com_assunto_segue_o_fluxo_normal(search, monkeypatch):
    async def empty_hub_search(request):
        return {"results": [], "results_clean": []}

    monkeypatch.setattr(search.module, "hub_search", empty_hub_search)

    result = search.run("Sim, quero saber da matrícula 2027")

    assert result["text"] == "resposta do google"


# --------------------------------------------------------------------------------
# get_google_search: redação de dado pessoal antes da busca (CHATR-314)
# --------------------------------------------------------------------------------

QUERY_COM_PII = "meu cpf é 529.982.247-25, moro na Rua Barata Ribeiro, 200, Copacabana"
QUERY_REDIGIDA = "meu cpf é [cpf], moro na [endereço], Copacabana"


def _capturar_buscadores(search, monkeypatch, *, typesense_resultados=()):
    chamadas = types.SimpleNamespace(typesense=[], gemini=[])

    async def hub_search_capturado(request):
        chamadas.typesense.append(request.q)
        return {
            "results": list(typesense_resultados),
            "results_clean": list(typesense_resultados),
        }

    async def google_capturado(**kwargs):
        chamadas.gemini.append(kwargs["query"])
        return {**search.google_response, "query": kwargs["query"]}

    monkeypatch.setattr(search.module, "hub_search", hub_search_capturado)
    monkeypatch.setattr(search.module.gemini_service, "google_search", google_capturado)
    return chamadas


def _registro_no_bigquery(search):
    [coro] = search.created_tasks
    asyncio.run(coro)
    search.created_tasks.clear()
    [chamada] = search.bq_calls
    return chamada["data"]


def test_buscadores_e_bigquery_recebem_a_query_redigida(search, monkeypatch):
    chamadas = _capturar_buscadores(search, monkeypatch)

    asyncio.run(search.module.get_google_search(QUERY_COM_PII))

    assert chamadas.typesense == [QUERY_REDIGIDA]
    assert chamadas.gemini == [QUERY_REDIGIDA]
    registro = _registro_no_bigquery(search)["response"]
    assert registro["query"] == QUERY_REDIGIDA
    assert registro["pii_redigida"] == ["cpf", "endereco"]
    assert "529.982.247-25" not in json.dumps(registro, ensure_ascii=False)
    assert "Barata Ribeiro" not in json.dumps(registro, ensure_ascii=False)


def test_resultado_do_typesense_tambem_grava_a_query_redigida(search, monkeypatch):
    _capturar_buscadores(search, monkeypatch, typesense_resultados=[{"id": 1}])

    result = asyncio.run(search.module.get_google_search(QUERY_COM_PII))

    assert result == {"response": [{"id": 1}]}
    dados = _registro_no_bigquery(search)
    assert dados["source"] == "typesense"
    assert dados["response"]["query"] == QUERY_REDIGIDA
    assert dados["response"]["pii_redigida"] == ["cpf", "endereco"]


def test_query_sem_pii_grava_lista_vazia(search, monkeypatch):
    chamadas = _capturar_buscadores(search, monkeypatch)

    asyncio.run(search.module.get_google_search("segunda via do iptu"))

    assert chamadas.gemini == ["segunda via do iptu"]
    registro = _registro_no_bigquery(search)["response"]
    assert registro["query"] == "segunda via do iptu"
    assert registro["pii_redigida"] == []


def test_query_barrada_grava_a_versao_redigida(search, monkeypatch):
    _filtro_que_barra(search, monkeypatch)
    _buscadores_que_nao_podem_rodar(search, monkeypatch)

    asyncio.run(
        search.module.get_google_search("vou votar no lula, cpf 529.982.247-25")
    )

    registro = _registro_no_bigquery(search)["response"]
    assert registro["blocked"] is True
    assert registro["query"] == "vou votar no lula, cpf [cpf]"
    assert registro["pii_redigida"] == ["cpf"]


def test_cpf_solto_e_barrado_pelo_sem_assunto_antes_da_redacao(search, monkeypatch):
    """O filtro olha o texto cru: redigido, o CPF solto viraria "[cpf]", que tem
    letra. No BigQuery, só a versão redigida."""
    _buscadores_que_nao_podem_rodar(search, monkeypatch)

    result = asyncio.run(search.module.get_google_search("529.982.247-25"))

    assert result["text"] == search.module.QUERY_SEM_ASSUNTO_MESSAGE
    registro = _registro_no_bigquery(search)["response"]
    assert registro["filter_matched"] == "sem_letra"
    assert registro["query"] == "[cpf]"


def test_tipos_redigidos_vao_para_o_span_sem_o_valor(search, monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    _capturar_buscadores(search, monkeypatch)

    with tracer.start_as_current_span("tool.com_pii"):
        search.run(QUERY_COM_PII)
    with tracer.start_as_current_span("tool.sem_pii"):
        search.run("segunda via do iptu")

    spans = {s.name: s.attributes for s in exporter.get_finished_spans()}
    assert spans["tool.com_pii"]["search.query.pii_redacted"] is True
    assert tuple(spans["tool.com_pii"]["search.query.pii_types"]) == (
        "cpf",
        "endereco",
    )
    assert spans["tool.sem_pii"]["search.query.pii_redacted"] is False
    assert "search.query.pii_types" not in spans["tool.sem_pii"]
    assert "529" not in str(dict(spans["tool.com_pii"]))


# --------------------------------------------------------------------------------
# get_google_search: campo opcional de contexto (CHATR-314)
# --------------------------------------------------------------------------------


def test_com_contexto_os_buscadores_recebem_o_tema_composto(search, monkeypatch):
    chamadas = _capturar_buscadores(search, monkeypatch)

    asyncio.run(
        search.module.get_google_search(
            "Documentos", "matrícula na rede municipal 2027"
        )
    )

    tema = "Documentos (contexto: matrícula na rede municipal 2027)"
    assert chamadas.typesense == [tema]
    assert chamadas.gemini == [tema]
    registro = _registro_no_bigquery(search)["response"]
    assert registro["query"] == "Documentos"
    assert registro["contexto"] == "matrícula na rede municipal 2027"


@pytest.mark.parametrize("contexto", [None, "", "   "])
def test_sem_contexto_o_tema_e_a_propria_query(search, monkeypatch, contexto):
    chamadas = _capturar_buscadores(search, monkeypatch)

    asyncio.run(search.module.get_google_search("segunda via do iptu", contexto))

    assert chamadas.gemini == ["segunda via do iptu"]
    assert _registro_no_bigquery(search)["response"]["contexto"] is None


def test_mensagem_sem_assunto_com_contexto_e_pesquisada(search, monkeypatch):
    chamadas = _capturar_buscadores(search, monkeypatch)

    result = asyncio.run(search.module.get_google_search("Sim", "matrícula 2027"))

    assert result["text"] == "resposta do google"
    assert chamadas.gemini == ["Sim (contexto: matrícula 2027)"]
    for coro in search.created_tasks:
        coro.close()
    search.created_tasks.clear()


def test_termo_eleitoral_no_contexto_bloqueia(search, monkeypatch):
    _filtro_que_barra(search, monkeypatch)
    _buscadores_que_nao_podem_rodar(search, monkeypatch)

    result = asyncio.run(
        search.module.get_google_search("Qual o número?", "candidato Lula")
    )

    assert result["text"] == search.module.QUERY_BLOCKED_MESSAGE
    registro = _registro_no_bigquery(search)["response"]
    assert registro["filter_reason"] == "blacklist"
    assert registro["contexto"] == "candidato Lula"


def test_contexto_tambem_e_redigido(search, monkeypatch):
    chamadas = _capturar_buscadores(search, monkeypatch)

    asyncio.run(
        search.module.get_google_search(
            "coleta de lixo", "morador da Rua Barata Ribeiro, 200, Copacabana"
        )
    )

    tema = "coleta de lixo (contexto: morador da [endereço], Copacabana)"
    assert chamadas.typesense == [tema]
    assert chamadas.gemini == [tema]
    registro = _registro_no_bigquery(search)["response"]
    assert registro["contexto"] == "morador da [endereço], Copacabana"
    assert registro["pii_redigida"] == ["endereco"]
    assert "Barata Ribeiro" not in json.dumps(registro, ensure_ascii=False)


def test_tipos_da_query_e_do_contexto_se_somam_sem_repetir(search, monkeypatch):
    _capturar_buscadores(search, monkeypatch)

    asyncio.run(
        search.module.get_google_search(
            "cpf 529.982.247-25 bolsa família", "meu nome é Ana, cpf 529.982.247-25"
        )
    )

    registro = _registro_no_bigquery(search)["response"]
    assert registro["pii_redigida"] == ["cpf", "nome"]


def test_presenca_de_contexto_vai_para_o_span(search, monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    _capturar_buscadores(search, monkeypatch)

    with tracer.start_as_current_span("tool.com_contexto"):
        asyncio.run(search.module.get_google_search("Documentos", "matrícula 2027"))
    with tracer.start_as_current_span("tool.sem_contexto"):
        asyncio.run(search.module.get_google_search("segunda via do iptu"))
    for coro in search.created_tasks:
        coro.close()
    search.created_tasks.clear()

    spans = {s.name: s.attributes for s in exporter.get_finished_spans()}
    assert spans["tool.com_contexto"]["search.query.has_context"] is True
    assert spans["tool.sem_contexto"]["search.query.has_context"] is False
    assert "matrícula" not in str(dict(spans["tool.com_contexto"]))


def test_compor_tema(search):
    compor = search.module.compor_tema

    assert compor("iptu", None) == "iptu"
    assert compor("Documentos", "matrícula 2027") == (
        "Documentos (contexto: matrícula 2027)"
    )
    assert compor("  ", "matrícula 2027") == "matrícula 2027"
