from typing import Dict, Any, List, Sequence
import asyncio
from google import genai
from google.genai.types import (
    Tool,
    UrlContext,
    ThinkingConfig,
    GenerateContentConfig,
    GoogleSearch,
    Content,
    Part,
    GenerateContentResponse,
)
import src.config.env as env
from src.config.gemini_search import (
    MIN_ATTEMPT_SECONDS,
    URL_RESOLUTION_RESERVE_SECONDS,
    SearchModelConfig,
    search_deadline_seconds,
)
from uuid import uuid4

from datetime import datetime
import httpx
import random
import time

from src.utils.log import logger
from src.utils.error_interceptor import interceptor, send_api_error
from src.utils.http_client import InterceptedHTTPClient
from google.genai import errors as genai_errors
from opentelemetry import trace

from src.observability.tracing import traced_stage
from urllib.parse import urljoin, urlparse

from src.tools.google_search import domain_blacklist as blacklist_de_dominios


# Códigos 4xx que, apesar de "erro do cliente", são transitórios: 429 é saturação de
# cota/capacidade e 408 é timeout do lado do servidor. Os demais 4xx não melhoram com
# repetição.
RETRYABLE_CLIENT_CODES = frozenset({408, 429})

# Mensagens devolvidas ao agente quando a busca não pode ser concluída. Substituem a
# exceção crua, que antes vazava para o usuário final (CHATR-122).
SEARCH_UNAVAILABLE_MESSAGE = (
    "A busca na web está temporariamente indisponível porque o serviço de pesquisa "
    "está sobrecarregado neste momento. Informe ao usuário que houve uma instabilidade "
    "momentânea e peça que tente novamente em alguns instantes. NÃO responda à "
    "pergunta com informações não verificadas."
)
SEARCH_FAILED_MESSAGE = (
    "Não foi possível concluir a busca na web neste momento por uma falha no serviço "
    "de pesquisa. Informe ao usuário que houve um problema momentâneo e peça que tente "
    "novamente em alguns instantes. NÃO responda à pergunta com informações não "
    "verificadas."
)

# Falhas de capacidade/latência: o usuário deve ouvir "instabilidade momentânea", não
# "erro". Qualquer outro tipo cai na mensagem genérica.
TRANSIENT_ERROR_KINDS = frozenset(
    {"gemini_unavailable", "gemini_rate_limited", "timeout", "deadline"}
)

# `URL_RESOLUTION_RESERVE_SECONDS` e `MIN_ATTEMPT_SECONDS` vêm de
# `src/config/gemini_search.py`, que valida a lista de modelos contra eles.
# Fração do budget de raciocínio a partir da qual a chamada é marcada como "bateu no
# teto". O Gemini para alguns tokens antes do valor configurado (8.190 de 8.192 nos
# testes de 2026-10-05).
REASONING_BUDGET_HIT_RATIO = 0.95
# `finish_reason` que não indicam falha. `None` e `FINISH_REASON_UNSPECIFIED` seguem o
# fluxo normal, como antes desta checagem existir.
FINISH_REASONS_OK = frozenset({None, "STOP", "FINISH_REASON_UNSPECIFIED"})


def classify_search_error(exc: BaseException) -> Dict[str, Any]:
    """Classifica a exceção de uma tentativa de busca.

    `genai.errors.APIError` expõe `code` (status HTTP) e `status` (ex.: "UNAVAILABLE").
    O 503/UNAVAILABLE — a saturação de capacidade do Gemini, com a mensagem "high
    demand" — ganha rótulo próprio para poder ser medido separado dos demais 5xx.
    """
    code = getattr(exc, "code", None)
    code = code if isinstance(code, int) else None
    status = getattr(exc, "status", None)
    status = status if isinstance(status, str) else None

    if isinstance(exc, genai_errors.ClientError):
        retryable = code in RETRYABLE_CLIENT_CODES
        kind = "gemini_rate_limited" if retryable else "gemini_client_error"
    elif isinstance(exc, genai_errors.ServerError):
        retryable = True
        kind = (
            "gemini_unavailable"
            if code == 503 or status == "UNAVAILABLE"
            else "gemini_server_error"
        )
    elif isinstance(exc, asyncio.TimeoutError):
        retryable = True
        kind = "timeout"
    else:
        retryable = True
        kind = "unexpected_error"

    return {"kind": kind, "status": status, "code": code, "retryable": retryable}


def server_retry_delay(exc: BaseException) -> float | None:
    """Extrai o `retryDelay` (google.rpc.RetryInfo) do corpo do erro, se houver.

    Quando a própria API informa quanto esperar, esse valor vale mais que o backoff
    que calculamos às cegas.
    """
    details = getattr(exc, "details", None)
    if not isinstance(details, dict):
        return None

    error = details.get("error")
    entries = error.get("details") if isinstance(error, dict) else None
    if not isinstance(entries, list):
        return None

    for entry in entries:
        if not isinstance(entry, dict) or "RetryInfo" not in str(
            entry.get("@type", "")
        ):
            continue
        raw = entry.get("retryDelay")
        if raw is None:
            continue
        try:
            return float(str(raw).rstrip("s"))
        except ValueError:
            logger.warning(f"retryDelay em formato inesperado: {raw!r}")

    return None


def backoff_seconds(attempt: int, base: float, cap: float) -> float:
    """Backoff exponencial com teto e *equal jitter*.

    O jitter existe para evitar o efeito manada: sem ele, todas as requisições que
    falharam no mesmo segundo voltariam juntas e recriariam o pico que causou o 503.
    """
    window = min(base * (2**attempt), cap)
    return window / 2 + random.uniform(0, window / 2)


def next_wait_seconds(
    exc: BaseException, attempt: int, base: float, cap: float
) -> float:
    """Quanto esperar antes da próxima tentativa.

    Quando a própria API informa o tempo de espera, esse valor vale mais que o backoff
    calculado às cegas — mas continua limitado pelo teto, para não travar o chat.
    """
    server_delay = server_retry_delay(exc)
    if server_delay is not None:
        return min(server_delay, cap)
    return backoff_seconds(attempt, base, cap)


def _deadline_error() -> Dict[str, Any]:
    return {"kind": "deadline", "status": None, "code": None, "retryable": False}


def _finish_reason_name(candidate: Any) -> str | None:
    """`FinishReason` do SDK como string (`"STOP"`, `"MAX_TOKENS"`…), ou `None`."""
    finish_reason = getattr(candidate, "finish_reason", None)
    if finish_reason is None:
        return None
    return getattr(finish_reason, "value", None) or str(finish_reason)


def _thinking_config(config: SearchModelConfig) -> ThinkingConfig:
    """Raciocínio por geração de modelo (CHATR-298): os 2.x usam budget de tokens
    (CHATR-234); os 3.x usam nível, e no `MINIMAL` não gastam raciocínio. A
    configuração já chega com exatamente um dos dois."""
    if config.thinking_budget is not None:
        return ThinkingConfig(thinking_budget=config.thinking_budget)
    return ThinkingConfig(thinking_level=config.thinking_level)


def _generation_config(config: SearchModelConfig) -> GenerateContentConfig:
    """Parâmetros da chamada, vindos da configuração do modelo (CHATR-298)."""
    return GenerateContentConfig(
        temperature=config.temperature,
        thinking_config=_thinking_config(config),
        max_output_tokens=config.max_output_tokens,
        tools=[
            Tool(google_search=GoogleSearch()),
            Tool(url_context=UrlContext()),
        ],
        response_mime_type="text/plain",
    )


def _marcar_span(**atributos: Any) -> None:
    """Grava atributos no span ativo; sem span, não faz nada."""
    span = trace.get_current_span()
    for chave, valor in atributos.items():
        if valor is not None:
            span.set_attribute(chave, valor)


def _registrar_geracao(
    tokens_metadata: Dict[str, Any],
    finish_reason: str | None,
    config: SearchModelConfig,
):
    """Publica contadores e `finish_reason` no span e no log; devolve se o
    raciocínio bateu no budget.

    Bater no budget não é falha: com o teto, o loop de raciocínio termina em ~30 s
    com uma resposta curta e `finish_reason=STOP`. Só fica marcado para medir.
    """
    # Os 3.x não têm budget: nunca "batem no teto".
    budget = config.thinking_budget or 0
    thoughts = tokens_metadata.get("thoughts_token_count")
    candidates = tokens_metadata.get("candidates_token_count")
    budget_hit = bool(
        budget > 0 and thoughts and thoughts >= budget * REASONING_BUDGET_HIT_RATIO
    )
    _marcar_span(
        **{
            "gemini.finish_reason": finish_reason,
            "gemini.thoughts_token_count": thoughts,
            "gemini.candidates_token_count": candidates,
            "gemini.reasoning_budget_hit": budget_hit,
        }
    )
    logger.info(
        f"Geração do Gemini | finish_reason={finish_reason} thoughts={thoughts} "
        f"resposta={candidates} reasoning_budget={budget} "
        f"max_output={config.max_output_tokens} "
        f"reasoning_budget_hit={budget_hit}"
    )
    return budget_hit


async def _resolver_urls_no_prazo(chunks: List[Any], deadline_at: float) -> dict:
    """Resolve as URLs das fontes em até `URL_RESOLUTION_RESERVE_SECONDS`.

    Antes ficava com tudo o que sobrava do prazo, e o p99 da etapa chegou a 30 s;
    com dois modelos isso estouraria o teto do Salesforce (CHATR-298). Se não der
    tempo, a resposta do Gemini não é descartada nem retentada: as fontes saem com a
    URI crua do grounding, o mesmo fallback que `process_link` usa quando um link
    falha.
    """
    restante = min(
        URL_RESOLUTION_RESERVE_SECONDS, max(deadline_at - time.monotonic(), 0.0)
    )
    try:
        # O span fica dentro do `try`: o estouro passa por ele (marcado como erro)
        # antes de virar o fallback abaixo.
        with traced_stage("gemini.resolve_urls") as stage:
            stage.set_attribute("gemini.search.url_count", len(chunks))
            async with asyncio.timeout(restante):
                return await resolve_urls(urls_to_resolve=chunks)
    except TimeoutError:
        logger.warning(
            f"Resolução de URLs estourou o prazo ({restante:.1f}s). "
            f"Seguindo com as URIs do grounding."
        )
        _marcar_span(**{"gemini.search.urls_degraded": True})
        return {
            chunk.web.uri: {"url": chunk.web.uri, "error": "prazo esgotado"}
            for chunk in chunks
        }


class GeminiService:
    def __init__(self):
        """Inicializa o cliente Gemini com as configurações do ambiente."""
        self.api_key = env.GEMINI_API_KEY
        self.client = genai.Client(api_key=self.api_key)

    def get_client(self):
        """Retorna a instância do cliente Gemini."""
        return self.client

    @interceptor(source={"source": "mcp", "tool": "gemini"})
    async def google_search(
        self,
        query: str,
        models: Sequence[SearchModelConfig] | None = None,
    ):
        """Busca com fallback entre modelos (CHATR-298).

        Cada modelo da lista tem o seu prazo. Um erro transitório (503, 429, 5xx)
        ganha uma nova tentativa curta no mesmo modelo; qualquer outra falha — prazo
        do modelo, `RECITATION`, `MAX_TOKENS`, resposta vazia, 4xx — passa direto
        para o próximo. Só quando todos falham a busca vira falha técnica e alerta.

        `models` substitui a lista do env (`GEMINI_SEARCH_MODELS`). O prazo total é a
        soma dos prazos dos modelos mais a reserva da resolução de URLs.
        """
        logger.info(f"Iniciando pesquisa Google para: {query}")
        request_id = str(uuid4())
        models = tuple(models or env.GEMINI_SEARCH_CONFIG.models)
        last_exception = None
        last_error = None
        # Contadores da última geração que falhou, para irem ao BigQuery.
        last_extra = None
        # Chamadas feitas ao Gemini, somando todos os modelos.
        calls_made = 0
        # Um item por modelo tentado, com o motivo de a busca ter passado adiante.
        models_tried: List[Dict[str, Any]] = []
        started_at = time.monotonic()
        deadline_s = search_deadline_seconds(models, URL_RESOLUTION_RESERVE_SECONDS)
        deadline_at = started_at + deadline_s
        backoff_cap = env.GEMINI_SEARCH_RETRY_MAX_BACKOFF_SECONDS
        formatted_prompt = web_searcher_instructions(research_topic=query)
        _marcar_span(
            **{
                "gemini.search.deadline_s": deadline_s,
                "gemini.search.models": ",".join(m.model for m in models),
            }
        )

        for model_index, model_config in enumerate(models):
            model = model_config.model
            attempts_per_model = model_config.attempts
            # O modelo fica com o próprio prazo, limitado pelo que sobra do total
            # depois de reservar o tempo da resolução de URLs, que vem no fim.
            model_deadline_at = min(
                time.monotonic() + model_config.timeout_s,
                deadline_at - URL_RESOLUTION_RESERVE_SECONDS,
            )
            if model_deadline_at - time.monotonic() < MIN_ATTEMPT_SECONDS:
                # O que falta é o prazo total, que vale igual para os próximos.
                logger.warning(
                    f"Prazo da busca não comporta o {model} "
                    f"({model_deadline_at - time.monotonic():.1f}s disponíveis). "
                    f"Desistindo."
                )
                last_error = last_error or _deadline_error()
                break
            model_error = None

            for attempt in range(attempts_per_model):
                model_seconds = model_deadline_at - time.monotonic()
                if model_seconds < MIN_ATTEMPT_SECONDS:
                    logger.warning(
                        f"Prazo da busca não comporta nova chamada ao {model} "
                        f"({model_seconds:.1f}s disponíveis). Desistindo do modelo."
                    )
                    model_error = model_error or _deadline_error()
                    break
                try:
                    logger.info(f"Prompt Length: {len(formatted_prompt)}")
                    calls_made += 1
                    # Span por fora do `timeout`: o estouro do prazo chega a ele como
                    # `TimeoutError` e fica marcado como erro da tentativa.
                    with traced_stage("gemini.generate_content") as stage:
                        stage.set_attribute("gemini.attempt", calls_made)
                        stage.set_attribute("gemini.model", model)
                        stage.set_attribute("gemini.model_index", model_index)
                        stage.set_attribute(
                            "gemini.model_deadline_s", model_config.timeout_s
                        )
                        stage.set_attribute(
                            "gemini.max_output_tokens", model_config.max_output_tokens
                        )
                        if model_config.thinking_budget is not None:
                            stage.set_attribute(
                                "gemini.reasoning_budget_tokens",
                                model_config.thinking_budget,
                            )
                        else:
                            stage.set_attribute(
                                "gemini.thinking_level", model_config.thinking_level
                            )
                        async with asyncio.timeout(model_seconds):
                            response = await self.client.aio.models.generate_content(
                                model=model,
                                contents=[
                                    Content(
                                        role="user",
                                        parts=[Part(text=formatted_prompt)],
                                    )
                                ],
                                config=_generation_config(model_config),
                            )
                except Exception as e:  # classificado logo abaixo
                    last_exception = e
                    last_extra = None
                    # O único timeout desta etapa é o do prazo do modelo.
                    model_error = (
                        _deadline_error()
                        if isinstance(e, TimeoutError)
                        else classify_search_error(e)
                    )
                    if not model_error["retryable"]:
                        logger.warning(
                            f"Erro não recuperável na API Google | model={model} "
                            f"({model_error['kind']}, code={model_error['code']}): "
                            f"{_descrever_excecao(e)}. Sem nova tentativa neste modelo."
                        )
                        break

                    logger.warning(
                        f"Erro transitório na API Google | model={model} "
                        f"({model_error['kind']}, code={model_error['code']}): "
                        f"{_descrever_excecao(e)}."
                    )
                    if attempt >= attempts_per_model - 1:
                        break

                    wait_time = next_wait_seconds(
                        e, attempt, env.GEMINI_SEARCH_RETRY_BASE_SECONDS, backoff_cap
                    )
                    # Não repetir uma chamada que não caberia no prazo do modelo:
                    # melhor sobrar o tempo para o próximo da lista.
                    remaining = model_deadline_at - time.monotonic()
                    if wait_time + MIN_ATTEMPT_SECONDS > remaining:
                        logger.warning(
                            f"Prazo do {model} não comporta nova tentativa: restam "
                            f"{remaining:.2f}s. Passando para o próximo modelo."
                        )
                        break

                    logger.info(
                        f"Tentativa {attempt + 1}/{attempts_per_model} no {model} "
                        f"falhou. Aguardando {wait_time:.2f}s para repetir."
                    )
                    await asyncio.sleep(wait_time)
                    continue

                logger.info(f"Resposta recebida do Gemini | model={model}")

                if not response.candidates:
                    logger.warning(f"Resposta sem candidatos válidos | model={model}")
                    model_error = {
                        "kind": "empty_response",
                        "status": None,
                        "code": None,
                        "retryable": False,
                    }
                    last_exception = None
                    last_extra = None
                    break

                candidate = response.candidates[0]
                tokens_metadata = self.get_tokens_metadata(response=response)
                finish_reason = _finish_reason_name(candidate)
                reasoning_budget_hit = _registrar_geracao(
                    tokens_metadata, finish_reason, model_config
                )
                geracao = {
                    "finish_reason": finish_reason,
                    "reasoning_budget_hit": reasoning_budget_hit,
                }

                # Antes do ramo "sem fonte": um loop cortado no teto costuma vir sem
                # `grounding_metadata`, e cairia lá como `success: True`. A mesma
                # pergunta tende a repetir o loop no mesmo modelo: vai para o próximo.
                # Variável própria: `model_error` pode trazer o 503 de uma tentativa
                # anterior deste modelo, e a resposta de agora é que está em jogo.
                descarte = None
                if finish_reason == "MAX_TOKENS":
                    descarte = {"kind": "max_tokens", "status": None, "code": None}
                elif finish_reason not in FINISH_REASONS_OK:
                    # RECITATION, SAFETY etc. vêm com texto vazio: não é "sem fonte".
                    # O RECITATION é quase sempre do modelo da vez: outro modelo
                    # respondeu às 36 perguntas que recitaram em prod no 2.5.
                    descarte = {
                        "kind": f"finish_reason:{finish_reason}",
                        "status": None,
                        "code": None,
                    }
                if descarte:
                    model_error = descarte
                    last_exception = None
                    last_extra = {**geracao, "tokens_metadata": tokens_metadata}
                    logger.warning(
                        f"Geração descartada | model={model} "
                        f"finish_reason={finish_reason}. Passando para o próximo modelo."
                    )
                    break

                models_tried.append({"model": model, "error_kind": None})
                return await self._resposta_com_fontes(
                    request_id=request_id,
                    query=query,
                    model=model,
                    temperature=model_config.temperature,
                    response=response,
                    candidate=candidate,
                    tokens_metadata=tokens_metadata,
                    geracao=geracao,
                    deadline_at=deadline_at,
                    meta={
                        "retry_attempts": calls_made - 1,
                        "models_tried": models_tried,
                        "fallback_used": model_index > 0,
                    },
                    started_at=started_at,
                )

            last_error = model_error
            models_tried.append({"model": model, "error_kind": model_error["kind"]})

        ultimo_modelo = models[len(models_tried) - 1] if models_tried else models[0]
        return await self._failed_search_response(
            request_id=request_id,
            query=query,
            model=ultimo_modelo.model,
            temperature=ultimo_modelo.temperature,
            attempts_used=calls_made,
            elapsed_ms=int((time.monotonic() - started_at) * 1000),
            last_error=last_error or _deadline_error(),
            last_exception=last_exception,
            models_tried=models_tried,
            extra=last_extra,
        )

    async def _resposta_com_fontes(
        self,
        *,
        request_id: str,
        query: str,
        model: str,
        temperature: float,
        response: GenerateContentResponse,
        candidate: Any,
        tokens_metadata: Dict[str, Any],
        geracao: Dict[str, Any],
        deadline_at: float,
        meta: Dict[str, Any],
        started_at: float,
    ) -> dict:
        """Monta a resposta de uma geração aceita: sem fonte, só fontes fora do
        escopo, ou o texto com citações."""
        base = {
            "id": request_id,
            "tokens_metadata": tokens_metadata,
            **geracao,
            **meta,
            "success": True,
            "model": model,
            "temperature": temperature,
            "query": query,
        }
        if meta["fallback_used"]:
            # Sem alerta: a busca foi salva. O sinal fica no log e no span.
            logger.warning(
                f"Busca salva pelo fallback de modelo | model={model} "
                f"models_tried={meta['models_tried']} "
                f"elapsed_ms={int((time.monotonic() - started_at) * 1000)}"
            )
        _marcar_span(
            **{
                "gemini.search.model_used": model,
                "gemini.search.fallback_used": meta["fallback_used"],
            }
        )

        # Check if grounding metadata and chunks exist
        if (
            not candidate.grounding_metadata
            or not candidate.grounding_metadata.grounding_chunks
        ):
            # No grounding chunks found - likely all sources were filtered or unavailable
            logger.warning("Nenhuma fonte confiável encontrada para a consulta")
            return {
                **base,
                "text": "Desculpe, mas não consegui encontrar informações confiáveis sobre este tópico em fontes oficiais. Esta consulta pode estar fora do escopo do meu conhecimento, que se concentra em serviços municipais do Rio de Janeiro e fontes governamentais oficiais.",
                "sources": [],
                "web_search_queries": [],
            }

        logger.info("Resolvendo URLs das fontes...")
        resolved_urls_map = await _resolver_urls_no_prazo(
            candidate.grounding_metadata.grounding_chunks, deadline_at
        )

        citations = get_citations(
            response=response, resolved_urls_map=resolved_urls_map
        )
        modified_text = format_text_with_citations(response.text, citations)
        sources_gathered = get_sources_list(citations, modified_text)
        web_search_queries = candidate.grounding_metadata.web_search_queries or []

        # Check if all sources were filtered out by blacklist
        if not sources_gathered and citations:
            logger.warning("Todas as fontes encontradas estão na blacklist")
            return {
                **base,
                "text": "Desculpe, mas as informações encontradas para esta consulta vêm de fontes que estão fora do escopo do meu conhecimento. Eu me concentro em fornecer informações sobre serviços municipais do Rio de Janeiro usando apenas fontes governamentais oficiais e confiáveis.",
                "sources": [],
                "web_search_queries": web_search_queries,
            }

        if meta["retry_attempts"] > 0:
            logger.info(
                f"Pesquisa Google recuperada após retry | "
                f"outcome=success attempts_used={meta['retry_attempts'] + 1} "
                f"model={model} "
                f"elapsed_ms={int((time.monotonic() - started_at) * 1000)}"
            )

        logger.info(f"Pesquisa concluída com {len(sources_gathered)} fontes")

        return {
            **base,
            "text": modified_text,
            "sources": sources_gathered,
            "web_search_queries": web_search_queries,
        }

    async def _failed_search_response(
        self,
        request_id: str,
        query: str,
        model: str,
        temperature: float,
        attempts_used: int,
        elapsed_ms: int,
        last_error: Dict[str, Any] | None,
        last_exception: BaseException | None,
        models_tried: List[Dict[str, Any]] | None = None,
        extra: Dict[str, Any] | None = None,
    ) -> dict:
        """Resposta devolvida quando a busca não pode ser concluída.

        Devolve uma mensagem tratada — nunca a exceção crua, que antes chegava ao
        usuário final — e reporta a falha ao interceptor. O report é explícito porque
        esta função *retorna* em vez de levantar, então o decorator `@interceptor` não
        enxergaria nada aqui. Quem transforma isto em `isError` é `get_google_search`,
        depois de gravar no BigQuery.
        """
        extra = dict(extra or {})
        tokens_metadata = extra.pop("tokens_metadata", {})
        error_kind = (last_error or {}).get("kind", "unknown")
        error_details = {
            "kind": error_kind,
            "status": (last_error or {}).get("status"),
            "code": (last_error or {}).get("code"),
            "attempts": attempts_used,
            "elapsed_ms": elapsed_ms,
            **extra,
        }
        # Nunca vazio: o timeout tem `str()` vazio e o `finish_reason` não tem
        # exceção — o alerta chegava como "Último erro: " ou "Último erro: None".
        ultimo_erro = (
            _descrever_excecao(last_exception) if last_exception else error_kind
        )

        logger.error(
            f"Pesquisa falhou após {attempts_used} tentativa(s) para a query: {query} | "
            f"outcome=failed error_kind={error_kind} "
            f"error_code={error_details['code']} elapsed_ms={elapsed_ms} "
            f"models_tried={models_tried} | Último erro: {ultimo_erro}"
        )

        await send_api_error(
            user_id="unknown",
            source={"source": "mcp", "tool": "gemini", "function": "google_search"},
            api_endpoint=f"gemini:{model}:generate_content",
            request_body={"query": query},
            status_code=error_details["code"] or 0,
            error_message=(
                f"{error_kind}: busca Google falhou após {attempts_used} tentativas "
                f"em {len(models_tried or [])} modelo(s) ({elapsed_ms}ms). "
                f"Último erro: {ultimo_erro}"
            ),
        )

        return {
            "id": request_id,
            "text": (
                SEARCH_UNAVAILABLE_MESSAGE
                if error_kind in TRANSIENT_ERROR_KINDS
                else SEARCH_FAILED_MESSAGE
            ),
            "sources": [],
            "web_search_queries": [],
            "tokens_metadata": tokens_metadata,
            "retry_attempts": attempts_used,
            "models_tried": models_tried or [],
            "fallback_used": False,
            "success": False,
            "error": error_details,
            "model": model,
            "temperature": temperature,
            "query": query,
        }

    def get_tokens_metadata(self, response: GenerateContentResponse) -> dict:
        usage_metadata = response.usage_metadata
        tokens_metadata = {}
        if usage_metadata:
            # Helper function to convert ModalityTokenCount to dict
            def convert_modality_token_count(item):
                if item is None:
                    return None
                if isinstance(item, list):
                    return [
                        (
                            {
                                "modality": tc.modality.value,
                                "token_count": tc.token_count,
                            }
                            if hasattr(tc, "modality")
                            else tc
                        )
                        for tc in item
                    ]
                if hasattr(item, "modality"):
                    return {
                        "modality": item.modality.value,
                        "token_count": item.token_count,
                    }
                return item

            tokens_metadata["cache_tokens_details"] = convert_modality_token_count(
                usage_metadata.cache_tokens_details
            )
            tokens_metadata["cached_content_token_count"] = (
                usage_metadata.cached_content_token_count
            )
            tokens_metadata["candidates_token_count"] = (
                usage_metadata.candidates_token_count
            )
            tokens_metadata["candidates_tokens_details"] = convert_modality_token_count(
                usage_metadata.candidates_tokens_details
            )
            tokens_metadata["prompt_token_count"] = usage_metadata.prompt_token_count
            tokens_metadata["thoughts_token_count"] = (
                usage_metadata.thoughts_token_count
            )
            tokens_metadata["tool_use_prompt_token_count"] = (
                usage_metadata.tool_use_prompt_token_count
            )
            tokens_metadata["tool_use_prompt_tokens_details"] = (
                convert_modality_token_count(
                    usage_metadata.tool_use_prompt_tokens_details
                )
            )
            tokens_metadata["total_token_count"] = usage_metadata.total_token_count

        return tokens_metadata


gemini_service = GeminiService()


def _descrever_excecao(exc: BaseException) -> str:
    """Tipo e mensagem da exceção, nunca vazio.

    Os timeouts do httpx (`ConnectTimeout`, `ReadTimeout`) têm `str()` vazio: sem o
    tipo, o alerta chegava como "Erro no HEAD: " sem nenhuma pista da causa.
    """
    mensagem = str(exc)
    nome = type(exc).__name__
    return f"{nome}: {mensagem}" if mensagem else nome


# Host dos links de redirecionamento que o grounding do Gemini devolve como fonte.
GROUNDING_REDIRECT_HOST = "vertexaisearch.cloud.google.com"
# O salto no Google levou de 0,08 a 0,3 s nas medições de 26/10/2026. O pior caso
# (as duas tentativas estourando o timeout, mais a espera entre elas) fica em
# 6,25 s, dentro da reserva de 10 s da resolução (`URL_RESOLUTION_RESERVE_SECONDS`).
GROUNDING_REDIRECT_TIMEOUT_SECONDS = 3.0
GROUNDING_REDIRECT_ATTEMPTS = 2
GROUNDING_REDIRECT_BACKOFF_SECONDS = 0.25


def _eh_link_do_grounding(uri: str | None) -> bool:
    try:
        return urlparse(uri or "").hostname == GROUNDING_REDIRECT_HOST
    except ValueError:
        return False


async def process_link(session, link: dict):
    """Resolve o link de redirecionamento do grounding pelo `Location` (CHATR-314).

    Faz uma requisição ao Google, sem seguir o redirecionamento: o destino já vem
    no `Location` da resposta 3xx, e `Location` relativo é resolvido contra o link
    do Google. O site de destino nunca é contatado. Antes, seguir até ele (HEAD e
    depois GET) custava p95 de ~10 s, e todo o ruído de alerta vinha de lá: TLS
    antigo, anti-robô, 404 de HEAD mal implementado.

    Link que não é do Google fica como está, sem requisição. Falha no salto do
    Google mantém o link do Google, que leva à mesma página; só 5xx e erro de
    transporte ganham uma nova tentativa curta.
    """
    uri = link.get("uri")
    link["url"] = uri
    link["error"] = None

    if not _eh_link_do_grounding(uri):
        return link

    async def report_final_error(status_code: int, error_message: str):
        await send_api_error(
            user_id="unknown",
            source={"source": "mcp", "tool": "gemini", "function": "resolve_urls"},
            api_endpoint=uri,
            request_body=None,
            status_code=status_code,
            error_message=error_message,
        )

    def should_report_resolution_error(
        status_code: int, error_message: str, exc: BaseException | None = None
    ) -> bool:
        """
        Classifica a falha do salto no Google. Desde o CHATR-314 o destino não é
        mais contatado, então nenhum alerta nasce do site de destino.

        O 429 é o rate limit do Google no endpoint de redirect para o nosso IP de
        saída, e o timeout é o redirect lento: os dois são esperados e a fonte sai
        com a URL de redirect, que funciona. Não há impacto para o cidadão
        (CHATR-299). 401/403/405 (bloqueio de robô) e erro de TLS seguem sem alerta.
        """
        normalized_error = (error_message or "").lower()

        if status_code in (401, 403, 405, 429):
            return False

        # Timeout é decidido pelo tipo, não pelo texto: o `str()` dos timeouts do
        # httpx é vazio, e procurar "timeout" na mensagem pegaria também o
        # "HTTP 504: Gateway Timeout", que é reportado de propósito.
        if isinstance(exc, httpx.TimeoutException):
            return False

        expected_transport_errors = (
            "certificate_verify_failed",
            "unable to get local issuer certificate",
            "server disconnected without sending a response",
            "remoteprotocolerror",
            "tlsv",
            "ssl:",
        )
        return not any(token in normalized_error for token in expected_transport_errors)

    async def registrar_falha(
        status_code: int, error_message: str, exc: BaseException | None = None
    ):
        """Reporta ao interceptor só a falha inesperada; a esperada fica no log,
        para o volume continuar mensurável. Nos dois casos a fonte fica com o link
        do Google."""
        if should_report_resolution_error(status_code, error_message, exc):
            await report_final_error(status_code, error_message)
        else:
            logger.warning(
                f"Falha esperada ao resolver URL do grounding, sem alerta | "
                f"status={status_code} erro={error_message}"
            )
        link["url"] = uri
        link["error"] = f"Erro ao resolver URL: {error_message}"

    for attempt in range(GROUNDING_REDIRECT_ATTEMPTS):
        is_last_attempt = attempt == GROUNDING_REDIRECT_ATTEMPTS - 1
        try:
            response = await session.get(
                uri,
                follow_redirects=False,
                timeout=GROUNDING_REDIRECT_TIMEOUT_SECONDS,
                intercept_errors=False,
            )
        except httpx.TransportError as e:
            # Conexão, TLS e timeout no Google: transitórios, uma nova tentativa.
            if not is_last_attempt:
                await asyncio.sleep(GROUNDING_REDIRECT_BACKOFF_SECONDS)
                continue
            await registrar_falha(0, _descrever_excecao(e), e)
            return link
        except Exception as e:
            await registrar_falha(0, _descrever_excecao(e), e)
            return link

        status_code = response.status_code
        location = response.headers.get("location", "").strip()
        if response.has_redirect_location and location:
            destino = urljoin(uri, location)
            if urlparse(destino).scheme in ("http", "https"):
                link["url"] = destino
                link["error"] = None
                return link
            await registrar_falha(status_code, f"HTTP {status_code}: Location inválido")
            return link

        if status_code >= 500 and not is_last_attempt:
            await asyncio.sleep(GROUNDING_REDIRECT_BACKOFF_SECONDS)
            continue

        if 300 <= status_code < 400:
            error_message = f"HTTP {status_code}: sem Location"
        elif status_code < 300:
            error_message = f"HTTP {status_code}: sem redirecionamento"
        else:
            error_message = (
                f"HTTP {status_code}: {response.reason_phrase or 'Empty Reason Phrase'}"
            )
        await registrar_falha(status_code, error_message)
        return link

    # Não alcançado: a última tentativa sempre retorna acima.
    return link


@interceptor(source={"source": "mcp", "tool": "gemini", "function": "resolve_urls"})
async def resolve_urls(urls_to_resolve: List[Any]) -> Dict[str, str]:
    """
    Create a map of the vertex ai search urls (very long) to a short url with a unique id for each url.
    Ensures each original URL gets a consistent shortened form while maintaining uniqueness.

    Desde o CHATR-314 cada link custa um salto no Google, sem seguir o
    redirecionamento: o destino sai do `Location` (ver `process_link`).
    """
    unique_urls = list(set([uri.web.uri for uri in urls_to_resolve]))
    urls = [{"uri": uri} for uri in unique_urls]

    # logger.info(f"Resolvendo {len(urls)} URLs únicas")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"
    }

    async with InterceptedHTTPClient(
        user_id="unknown",
        source={"source": "mcp", "tool": "gemini", "function": "resolve_urls"},
        timeout=GROUNDING_REDIRECT_TIMEOUT_SECONDS,
        follow_redirects=False,
        headers=headers,
    ) as session:
        # Limita concorrência para evitar sobrecarga
        semaphore = asyncio.Semaphore(20)

        async def process_with_semaphore(link):
            async with semaphore:
                return await process_link(session, link)

        results = await asyncio.gather(
            *(process_with_semaphore(link) for link in urls), return_exceptions=True
        )

        # Trata exceções não capturadas
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                # logger.error(
                #     f"Erro não tratado ao processar URL {urls[i]['uri']}: {result}"
                # )
                urls[i]["url"] = urls[i]["uri"]
                urls[i]["error"] = str(result)
            else:
                urls[i] = result

    resolved_map = {
        link["uri"]: {"url": link["url"], "error": link["error"]} for link in urls
    }

    successful_resolutions = sum(1 for link in urls if link["error"] is None)
    logger.info(f"URLs resolvidas com sucesso: {successful_resolutions}/{len(urls)}")

    return resolved_map


def _url_bloqueada(url: str | None) -> str | None:
    """Entrada da `domain_blacklist.yaml` que bloqueia a URL, ou `None` (CHATR-314).

    Substitui a `LINK_BLACKLIST`, que comparava por trecho de texto: `x.com`
    barrava `netflix.com`. Agora é domínio igual ou subdomínio.
    """
    return blacklist_de_dominios.domain_blacklist.url_bloqueada(url)


def get_citations(response, resolved_urls_map):
    """
    Extracts and formats citation information from a Gemini model's response.

    This function processes the grounding metadata provided in the response to
    construct a list of citation objects. Each citation object includes the
    start and end indices of the text segment it refers to, and a string
    containing formatted markdown links to the supporting web chunks.
    """
    citations = []

    # Ensure response and necessary nested structures are present
    if not response or not response.candidates:
        return citations

    candidate = response.candidates[0]
    if (
        not hasattr(candidate, "grounding_metadata")
        or not candidate.grounding_metadata
        or not hasattr(candidate.grounding_metadata, "grounding_supports")
    ):
        return citations

    for support in candidate.grounding_metadata.grounding_supports:
        citation = {}

        # Ensure segment information is present
        if not hasattr(support, "segment") or support.segment is None:
            continue  # Skip this support if segment info is missing

        start_index = (
            support.segment.start_index
            if support.segment.start_index is not None
            else 0
        )

        # Ensure end_index is present to form a valid segment
        if support.segment.end_index is None:
            continue  # Skip if end_index is missing, as it's crucial

        # Add 1 to end_index to make it an exclusive end for slicing/range purposes
        # (assuming the API provides an inclusive end_index)
        citation["start_index"] = start_index
        citation["end_index"] = support.segment.end_index

        citation["segments"] = []
        if (
            hasattr(support, "grounding_chunk_indices")
            and support.grounding_chunk_indices
        ):
            for ind in support.grounding_chunk_indices:
                try:
                    chunk = candidate.grounding_metadata.grounding_chunks[ind]
                    resolved_url = resolved_urls_map.get(chunk.web.uri, None)

                    # Skip if resolved_url is None or doesn't have the expected structure
                    if not resolved_url or "url" not in resolved_url:
                        continue

                    # Fonte de domínio bloqueado não vira citação (CHATR-314).
                    if _url_bloqueada(resolved_url["url"]):
                        continue

                    citation["segments"].append(
                        {
                            "label": chunk.web.title,
                            "uri": chunk.web.uri,
                            "url": resolved_url["url"],
                            # "text": support.segment.text,
                            # "error": resolved_url["error"],
                        }
                    )
                except (IndexError, AttributeError, NameError):
                    # Handle cases where chunk, web, uri, or resolved_map might be problematic
                    # For simplicity, we'll just skip adding this particular segment link
                    # In a production system, you might want to log this.
                    pass
        citations.append(citation)
    return citations


def format_text_with_citations(text, citations_data):
    """
    Insere marcadores de citação em um texto de forma inteligente,
    posicionando-os no final das palavras ou frases.

    Args:
        text (str): O texto original.
        citations_data (list): Uma lista de dicionários com os dados da citação.

    Returns:
        str: O texto modificado com as citações e a lista de fontes.
    """
    modified_text = text

    # 1. Mapear URLs para números de citação únicos para evitar duplicatas.
    # Usamos o 'index' fornecido nos dados ou criamos um novo se não houver.
    source_map = {}
    next_citation_num = 1
    # Pré-processa para atribuir números de citação únicos e estáveis
    temp_citations = []
    for citation in citations_data:
        # A maioria dos segmentos tem um 'index' que podemos usar
        try:
            # Skip citations with no segments (filtered out by blacklist)
            if not citation["segments"]:
                continue

            url = citation["segments"][0]["url"]
            if url not in source_map:
                source_map[url] = citation["segments"][0].get(
                    "index", next_citation_num
                )
                if "index" not in citation["segments"][0]:
                    next_citation_num += 1

            # Adiciona a citação com o número correto para processamento
            citation_num = source_map[url]
            temp_citations.append({**citation, "citation_num": citation_num})
        except (KeyError, IndexError):
            # Ignora citações malformadas ou vazias
            continue

    # 2. Ordenar as citações pelo 'end_index' em ordem DECRESCENTE.
    # Isso é CRUCIAL para que as inserções não afetem os índices das próximas.
    sorted_citations = sorted(
        temp_citations, key=lambda c: c["end_index"], reverse=True
    )

    # 3. Iterar e inserir os marcadores de citação
    for citation in sorted_citations:
        end_index = citation["end_index"]
        citation_num = citation["citation_num"]

        # O marcador a ser inserido (com um espaço antes para formatação)
        marker = f" [{citation_num}]"

        # 4. Encontrar a posição de inserção ideal
        # Começa no end_index e avança até o final da palavra (encontra um espaço, pontuação, etc.)
        insertion_pos = end_index
        while (
            insertion_pos < len(modified_text)
            and modified_text[insertion_pos].isalnum()
        ):
            insertion_pos += 1

        # Insere o marcador na posição encontrada
        modified_text = (
            modified_text[:insertion_pos] + marker + modified_text[insertion_pos:]
        )

    # 5. Gerar a lista de fontes formatada no final
    sources_list = "\n\n**Sources:**\n"
    # Inverte o mapa para ordenar por número de citação
    sorted_sources = sorted(source_map.items(), key=lambda item: item[1])

    for url, num in sorted_sources:
        # Pega o 'label' da primeira vez que a fonte apareceu
        next(
            (
                c["segments"][0]["label"]
                for c in citations_data
                if c["segments"] and c["segments"][0]["url"] == url
            ),
            url,
        )
        sources_list += f" - [{num}] {url}\n"

    return modified_text + sources_list


def get_sources_list(citations_data, modified_text):
    all_segments = (
        segment for citation in citations_data for segment in citation["segments"]
    )
    seen_uris = set()
    sources_gathered = []

    for source in all_segments:
        if source["uri"] not in seen_uris and source["url"] in modified_text:
            # Fonte de domínio bloqueado não entra na lista (CHATR-314).
            if not _url_bloqueada(source["url"]):
                seen_uris.add(source["uri"])
                sources_gathered.append(source)

    for index, source in enumerate(sources_gathered):
        source["index"] = index + 1

    return sources_gathered


def web_searcher_instructions(research_topic: str):
    current_date = datetime.now().strftime("%Y-%m-%d")

    return f"""### Persona
You are a diligent and meticulous Investigative Research Analyst specializing in Rio de Janeiro municipal government services. Your superpower is sifting through web search results to find the ground truth. You are skeptical, fact-driven, and obsessed with source integrity. You always use the `google_search` tool to find the ground truth using at least 5 different queries.

### Objective
Your mission is to execute a web search query, critically evaluate the top results, and synthesize the findings into a clear, factual, and perfectly cited text artifact. The quality of your work is the foundation for all subsequent analysis.

### Execution Workflow
You must follow this precise four-step process for every query:

1.  **Analyze Query Intent:** Before anything else, understand the specific goal of the provided query. This context will guide your evaluation.

2.  **Evaluate Search Results:** As you process the search results, you must act as a critical filter. Prioritize and weigh information based on these criteria:
    *   **Authority & Trustworthiness:** Strongly prefer official Rio de Janeiro municipal sources, like official domains (**carioca.rio, prefeitura.rio, 1746.rio, cor.rio, .gov.br**), official city departments, and municipal secretariats. \
        For electronic ticketing for public transport, or "RioCard" consider https://jae.com.br/central-de-ajuda/ as a source. \
        Whenever the user's question is about getting, renewing, requesting a duplicate, or finding service locations for the "Jae" card/"Riocard", the answer **must always** include the official link, which is: https://jae.com.br/onde-estamos/. \
        For workshops, courses and events related to Basic Computing, Creative Economy, Information Technologies, Robotics and Programming, Work and Entrepreneurship and Artificial Intelligence consider https://www.navedoconhecimento.rio/ as a source. \
        Do NOT quote the program RJPet/website rjpet.com.br as it does not exist anymore. \
        Informations about subprefecture should be searched in this official website: https://gbp.prefeitura.rio/subprefeituras/. \
        When providing guidance on how to pay traffic fines, you should always point out that there are three different types of violations: municipal, state, and federal. \
        To help users access their fines, always include the following links: Municipal Fines: https://carioca.rio/servicos/multa-de-transito-informacoes-sobre-como-pagar/ ; State Fines: https://www.detran.rj.gov.br/infracoes/pagamento-de-multas/como-pagar.html ; Federal Fines: https://servicos.dnit.gov.br/multas/ ; \
        Also, always mention that there is a mobile app called 'CDT - Carteira Digital de Trânsito' that allows you to check all three types of violations. \
        Secondary preference for established news sources covering Rio municipal affairs. \
        Avoid unofficial blogs, forums, opinion pieces, or generalist portals. \
        When login via Gov.br is available, inform the user about it, as it is the official government authentication system. \

    *   **Recency:** Check the publication date. For the given query, is older information still relevant, or is it critical to find the most recent data? The current date is **{current_date}**.
    *   **Objectivity:** Differentiate between factual reporting and biased commentary. Your synthesis must be based on facts.

3.  **Synthesize & Extract:** Consolidate the verified facts from the most reliable sources into an extensive and detailed summary.

4.  **Integrate Citations Flawlessly:** As you write, you MUST cite your sources.
    *   Use citations in an academic style, such as [source_name], immediately after the relevant fact or statement.
    *   If an entire paragraph is synthesized from a single source, you may place a single citation at the end of that paragraph.
    *   Your final answer is only credible if it is fully verifiable. Every key fact must be traceable to its source.

### Critical Directives
- **ZERO HALLUCINATION:** NEVER invent information, statistics, or sources. If the search results for the query are empty, inconclusive, or of poor quality, you MUST state: "No reliable information could be found for this query."
- **STRICT NEUTRALITY:** Your job is to report the facts, not interpret them. Do not add your own analysis, conclusions, or opinions. Present the synthesized information in a neutral, encyclopedic tone.
- **FOCUS ON THE QUERY:** Your synthesis must ONLY answer the specific research topic provided. Do not include interesting but tangential information from the sources. Stick to the mission.

### Output Format
A long, detailed markdown "Research Artifact" with all the information from the sources and the citations and the date of the search.


### Your Turn

**Research Topic:**
{research_topic}
"""
