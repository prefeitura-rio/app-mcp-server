"""
Error Interceptor Utility

Utilitário para enviar erros de API e outros erros para o sistema de monitoramento
via endpoint de error interceptor.

Este módulo fornece funções assíncronas para reportar erros de forma não-bloqueante,
garantindo que falhas no envio de erros não afetem o fluxo principal da aplicação.
"""

import asyncio
import inspect
import json
import re
import traceback as tb
from typing import Any, Callable, Dict, Optional, Set

import httpx
from loguru import logger
from opentelemetry import trace

from src.config import env
from src.utils.json_utils import CustomJSONEncoder


# ---------------------------------------------------------------------------
# Correlação com traces do OpenTelemetry (CHATR-113)
# ---------------------------------------------------------------------------
#
# Reusa exatamente o mesmo padrão de acesso ao contexto de trace/span usado
# em `src/observability/tracing.py`: `opentelemetry.trace.get_current_span()`
# seguido de `.get_span_context()`. Isso permite correlacionar um erro
# reportado ao interceptor com o trace correspondente no SigNoz (CHATR-110),
# sem introduzir nenhuma dependência nova nem exigir que o tracing esteja
# habilitado -- quando não há span ativo/válido, o contexto é simplesmente
# omitido do payload.


def _get_current_trace_context() -> Dict[str, str]:
    """
    Obtém trace_id/span_id (em hexadecimal, mesmo formato usado pelo
    OTel/SigNoz) do span atualmente ativo, se houver.

    Degrada graciosamente em qualquer cenário sem trace ativo (tracing não
    configurado, span inválido, ou qualquer erro inesperado ao acessar o
    contexto): nunca levanta exceção, apenas retorna um dicionário vazio.

    Returns:
        Dict com as chaves "trace_id" (32 caracteres hex) e "span_id" (16
        caracteres hex) se houver um span ativo válido, ou `{}` caso
        contrário.
    """
    try:
        span_context = trace.get_current_span().get_span_context()
        if not span_context.is_valid:
            return {}
        return {
            "trace_id": trace.format_trace_id(span_context.trace_id),
            "span_id": trace.format_span_id(span_context.span_id),
        }
    except Exception:
        # Nunca deixamos um problema de acesso ao contexto de trace impedir
        # o envio do relatório de erro em si.
        return {}


# ---------------------------------------------------------------------------
# Redação básica de PII (CHATR-113)
# ---------------------------------------------------------------------------
#
# Redação conservadora e best-effort, usando apenas stdlib (`re` + slicing
# de strings) -- não é uma limpeza completa do conteúdo, e sim uma redução
# do risco de exposição mantendo contexto suficiente para debug. Como
# `input_body` chega aqui já serializado como string (ver
# `send_error_to_interceptor` abaixo), a substituição por regex opera sobre
# texto puro e não é ciente da estrutura JSON subjacente -- em casos raros
# (ex.: um número aparecendo como literal numérico não citado dentro do
# JSON) a substituição poderia invalidar o JSON resultante; esse é um
# tradeoff aceito para manter a implementação simples e sem dependências
# novas.

# Formato de CPF "000.000.000-00". Esse padrão só pode aparecer dentro de
# uma string JSON válida (não é um literal numérico válido em JSON), então
# a substituição abaixo nunca corrompe a estrutura do payload.
_CPF_PATTERN = re.compile(r"(?<!\d)\d{3}\.\d{3}\.\d{3}-\d{2}(?!\d)")

# Números de telefone com 10 a 13 dígitos, com ou sem "+" inicial (cobre os
# formatos usados neste projeto, ex.: "5521999999999" ou "+5521999999999").
_PHONE_NUMBER_PATTERN = re.compile(r"(?<!\d)\+?\d{10,13}(?!\d)")


def _mask_last_four_digits(value: str) -> str:
    """
    Mascara um valor mantendo visíveis apenas os últimos 4 caracteres,
    substituindo o restante por asteriscos. Um eventual "+" inicial (comum
    em números de telefone no formato E.164) é preservado como está, já
    que não carrega PII por si só.

    Usado diretamente em `customer_whatsapp_number`: como esse campo é, por
    contrato, sempre um número de telefone, mascaramos incondicionalmente
    em vez de depender de `_PHONE_NUMBER_PATTERN` bater com o formato
    exato -- isso evita vazar o valor por completo caso ele venha em um
    formato inesperado (mais curto/mais longo que o previsto pelo regex).

    Examples:
        >>> _mask_last_four_digits("5521999999999")
        '*********9999'
        >>> _mask_last_four_digits("+5521999999999")
        '+*********9999'
    """
    if not value:
        return value
    prefix = "+" if value.startswith("+") else ""
    rest = value[len(prefix) :]
    if len(rest) <= 4:
        return value
    return f"{prefix}{'*' * (len(rest) - 4)}{rest[-4:]}"


def _redact_pii_in_text(text: str) -> str:
    """
    Aplica uma varredura leve por padrões óbvios de PII (CPF e números de
    telefone) em um texto livre, substituindo apenas os trechos que batem
    com esses padrões por um marcador -- o restante do conteúdo é mantido
    intacto, propositalmente, já que o objetivo é reduzir exposição de PII
    mantendo contexto útil de debug (isto não é uma limpeza completa do
    conteúdo).

    Usa o mesmo `_PHONE_NUMBER_PATTERN` usado para mascarar
    `customer_whatsapp_number`, então qualquer número de telefone que
    apareça embutido em `input_body` é redigido com a mesma referência de
    formato.
    """
    if not text:
        return text
    redacted = _CPF_PATTERN.sub("[REDACTED-CPF]", text)
    redacted = _PHONE_NUMBER_PATTERN.sub("[REDACTED-PHONE]", redacted)
    return redacted


async def send_error_to_interceptor(
    customer_whatsapp_number: str,
    flowname: str,
    api_endpoint: str,
    input_body: Any,
    http_status_code: int,
    error_message: str,
    traceback: Optional[str] = None,
    source: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Envia um erro para o error interceptor de forma assíncrona.

    Args:
        customer_whatsapp_number: ID do usuário do WhatsApp (user_id)
        flowname: Nome do fluxo no formato "multi_step_service(service_name)" ou "tool_name"
        api_endpoint: URL do endpoint que foi chamado
        input_body: Body que foi enviado na chamada (será serializado para JSON)
        http_status_code: Código HTTP de erro retornado
        error_message: Mensagem de erro principal
        traceback: Stack trace do erro (opcional)
        source: Fonte do erro (padrão: "mcp")

    Returns:
        True se o erro foi enviado com sucesso, False caso contrário

    Example:
        >>> await send_error_to_interceptor(
        ...     customer_whatsapp_number="5521999999999",
        ...     flowname="multi_step_service(iptu_pagamento)",
        ...     api_endpoint="https://api.example.com/iptu/consultar",
        ...     input_body={"inscricao": "12345678"},
        ...     http_status_code=500,
        ...     error_message="Internal Server Error",
        ...     traceback="Traceback (most recent call last):\\n..."
        ... )
        True
    """

    # Valida se as configurações estão disponíveis
    if not env.ERROR_INTERCEPTOR_URL or not env.ERROR_INTERCEPTOR_TOKEN:
        logger.warning(
            "Error Interceptor não configurado (URL ou TOKEN ausente). "
            "Erro não será reportado ao sistema de monitoramento."
        )
        return False

    # Serializa input_body para string JSON se necessário
    if isinstance(input_body, (dict, list)):
        input_body_str = json.dumps(
            input_body, ensure_ascii=False, cls=CustomJSONEncoder
        )
    elif input_body is None:
        input_body_str = "None"
    else:
        input_body_str = str(input_body)

    # Redação básica de PII antes do envio (CHATR-113): mascara o número de
    # telefone e redige possíveis CPFs/telefones embutidos em input_body.
    masked_whatsapp_number = _mask_last_four_digits(str(customer_whatsapp_number))
    input_body_str = _redact_pii_in_text(input_body_str)

    # Prepara error_response como JSON string contendo error_message e traceback
    error_response_data = {"error_message": error_message}
    if traceback:
        error_response_data["traceback"] = traceback
    error_response_str = json.dumps(error_response_data, ensure_ascii=False)

    # Serializa source como JSON string se for dicionário
    if source is not None:
        source_str = json.dumps(source, indent=2, ensure_ascii=False)
    else:
        source_str = "mcp"

    # Prepara o payload
    payload = {
        "customer_whatsapp_number": masked_whatsapp_number,
        "source": source_str,
        "flowname": flowname,
        "api_endpoint": api_endpoint,
        "input_body": input_body_str,
        "http_status_code": http_status_code,
        "error_response": error_response_str,
    }
    # Correlação com trace/span do OTel (CHATR-110/CHATR-113): presente
    # apenas quando há um trace ativo, ausente do payload caso contrário.
    payload.update(_get_current_trace_context())

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                env.ERROR_INTERCEPTOR_URL,
                json=payload,
                headers={
                    "accept": "application/json",
                    "x-api-key": env.ERROR_INTERCEPTOR_TOKEN,
                    "Content-Type": "application/json",
                },
            )

            if response.status_code == 200:
                logger.info(
                    f"✅ Erro reportado ao interceptor: {flowname} | "
                    f"Endpoint: {api_endpoint} | Status: {http_status_code}"
                )
                return True
            else:
                logger.warning(
                    f"⚠️ Falha ao reportar erro ao interceptor. "
                    f"Status: {response.status_code} | Response: {response.text[:200]}"
                )
                return False

    except httpx.TimeoutException:
        logger.warning(
            "⚠️ Timeout ao enviar erro para o interceptor. Continuando execução normal."
        )
        return False
    except Exception as e:
        logger.warning(
            f"⚠️ Erro ao enviar erro para o interceptor: {str(e)}. "
            "Continuando execução normal."
        )
        return False


def serialize_source(source: Dict[str, Any]) -> str:
    """
    Serializa qualquer dicionário source para uma string legível.

    Formato: key=value | key=value | ...
    Dicionários nested são serializados recursivamente.

    Args:
        source: Dicionário com qualquer formato

    Returns:
        String formatada para o flowname

    Examples:
        >>> serialize_source({"source": "mcp", "tool": "search"})
        'source=mcp | tool=search'

        >>> serialize_source({"source": "mcp", "tool": "multi_step_service", "workflow": "iptu_pagamento"})
        'source=mcp | tool=multi_step_service | workflow=iptu_pagamento'

        >>> serialize_source({"source": "mcp", "tool": "equipments", "function": "get_nearby", "address": "Av. Vargas"})
        'source=mcp | tool=equipments | function=get_nearby | address=Av. Vargas'
    """
    parts = []

    def add_value(key: str, value: Any):
        if isinstance(value, dict):
            # Para dicts nested, adiciona cada chave com prefixo
            for k, v in value.items():
                add_value(f"{key}.{k}", v)
        else:
            # Limita o tamanho do valor para evitar flownames gigantes
            str_value = str(value)[:50] if value is not None else ""
            parts.append(f"{key}={str_value}")

    for key, value in source.items():
        add_value(key, value)

    return " | ".join(parts)


async def send_api_error(
    user_id: str,
    source: Dict[str, Any],
    api_endpoint: str,
    request_body: Any,
    status_code: int,
    error_message: str,
    traceback: Optional[str] = None,
) -> bool:
    """
    Envia erros de API HTTP para o interceptor.

    Args:
        user_id: ID do usuário (WhatsApp number)
        source: Dicionário com qualquer formato identificando a origem do erro
        api_endpoint: URL do endpoint da API que falhou
        request_body: Body da requisição que causou erro
        status_code: Código HTTP de erro
        error_message: Mensagem de erro
        traceback: Stack trace do erro (opcional)

    Returns:
        True se reportado com sucesso, False caso contrário

    Example - Workflow:
        >>> await send_api_error(
        ...     user_id="5521999999999",
        ...     source={
        ...         "source": "mcp",
        ...         "tool": "multi_step_service",
        ...         "workflow": "iptu_pagamento",
        ...         "step": "consultar_guias"
        ...     },
        ...     api_endpoint="https://api.dados.rio/iptu/consultar",
        ...     request_body={"inscricao": "12345678"},
        ...     status_code=503,
        ...     error_message="Service Unavailable",
        ... )
        # Flowname será gerado via serialize_source(source)

    Example - Tool simples:
        >>> await send_api_error(
        ...     user_id="5521999999999",
        ...     source={"source": "mcp", "tool": "search"},
        ...     api_endpoint="https://api.typesense.io/search",
        ...     request_body={"q": "teste"},
        ...     status_code=500,
        ...     error_message="Internal Error",
        ... )
    """

    flowname = serialize_source(source)

    return await send_error_to_interceptor(
        customer_whatsapp_number=user_id,
        flowname=flowname,
        api_endpoint=api_endpoint,
        input_body=request_body,
        http_status_code=status_code,
        error_message=error_message,
        traceback=traceback,
        source=source,
    )


async def send_general_error(
    user_id: str,
    source: Dict[str, Any],
    error_type: str,
    error_message: str,
    traceback: Optional[str] = None,
    http_status_code: int = 0,
    input_body: Optional[Any] = None,
) -> bool:
    """
    Envia erros gerais (não relacionados a APIs externas) para o interceptor.

    Args:
        user_id: ID do usuário (WhatsApp number)
        source: Dicionário com qualquer formato identificando a origem do erro
        error_type: Tipo do erro (ex: "ValidationError", "ProcessingError")
        error_message: Mensagem de erro
        traceback: Stack trace do erro (opcional)
        http_status_code: Código de status (0 para erros internos)
        input_body: Parâmetros passados para a função (opcional)

    Returns:
        True se reportado com sucesso, False caso contrário

    Example - Tool simples:
        >>> await send_general_error(
        ...     user_id="5521999999999",
        ...     source={"source": "mcp", "tool": "search"},
        ...     error_type="TimeoutError",
        ...     error_message="Busca demorou demais",
        ... )
        # Flowname será gerado via serialize_source(source)

    Example - Workflow:
        >>> await send_general_error(
        ...     user_id="5521999999999",
        ...     source={
        ...         "source": "mcp",
        ...         "tool": "multi_step_service",
        ...         "workflow": "iptu_pagamento",
        ...         "step": "validar_inscricao"
        ...     },
        ...     error_type="ValidationError",
        ...     error_message="Inscrição imobiliária inválida",
        ... )
    """

    flowname = serialize_source(source)

    return await send_error_to_interceptor(
        customer_whatsapp_number=user_id,
        flowname=flowname,
        api_endpoint=f"internal://{error_type}",
        input_body=input_body if input_body is not None else source,
        http_status_code=http_status_code,
        error_message=error_message,
        traceback=traceback,
        source=source,
    )


# ---------------------------------------------------------------------------
# Tracking de tasks fire-and-forget do interceptor (CHATR-113)
# ---------------------------------------------------------------------------
#
# `sync_wrapper` (abaixo) agenda `_handle_error(...)` via
# `loop.create_task(...)` sem dar `await`, pois o contrato do decorator é
# nunca bloquear o chamador original. O problema conhecido do asyncio é que
# a *event loop* só guarda uma referência fraca a cada task -- sem uma
# referência forte guardada em algum outro lugar, o garbage collector pode
# coletar a task no meio da execução (perdendo o report silenciosamente,
# sem nenhuma exceção ou log). Este set guarda essa referência forte
# enquanto a task está pendente; o próprio callback de conclusão se
# encarrega de removê-la.
_pending_interceptor_tasks: Set[asyncio.Task] = set()


def _track_interceptor_task(task: asyncio.Task) -> None:
    """
    Registra uma task fire-and-forget do interceptor para que não seja
    coletada pelo GC antes de terminar, e loga (sem propagar) qualquer
    exceção não tratada ao concluir.

    Continua fire-and-forget: nada aqui bloqueia ou aguarda o chamador
    original, apenas torna a task observável até sua conclusão.
    """
    _pending_interceptor_tasks.add(task)

    def _on_done(finished_task: asyncio.Task) -> None:
        _pending_interceptor_tasks.discard(finished_task)
        if finished_task.cancelled():
            return
        exc = finished_task.exception()
        if exc is not None:
            logger.warning(f"Task de report ao error interceptor falhou: {exc}")

    task.add_done_callback(_on_done)


def interceptor(
    source: Dict[str, Any],
    error_types: tuple = (Exception,),
    extract_user_id: Optional[Callable] = None,
    extract_source: Optional[Callable] = None,
):
    """
    Decorator para capturar e reportar erros automaticamente.

    Este decorator intercepta exceções, envia para o sistema de monitoramento
    e re-levanta a exceção para que o fluxo normal de tratamento de erros continue.

    Suporta funções sync e async automaticamente.

    Args:
        source: Dicionário base identificando a origem do erro (qualquer formato)
        error_types: Tuple de tipos de exceção a interceptar (padrão: (Exception,))
        extract_user_id: Função para extrair user_id: (args, kwargs) -> str
        extract_source: Função para extrair/modificar source: (args, kwargs, base_source) -> dict
            Permite adicionar informações dinâmicas ao source base.

    Returns:
        Decorator que intercepta erros

    Example - Tool simples:
        >>> @interceptor(source={"source": "mcp", "tool": "search"})
        ... async def get_google_search(query: str):
        ...     return await do_search(query)
        # Flowname: "source=mcp | tool=search | function=get_google_search"

    Example - Workflow:
        >>> @interceptor(
        ...     source={"source": "mcp", "tool": "multi_step_service", "workflow": "iptu_pagamento"},
        ...     extract_user_id=lambda args, kwargs: args[0].user_id,
        ... )
        ... async def consultar_guias(self, inscricao: str):
        ...     return await self._fetch_guias(inscricao)
        # Flowname: "source=mcp | tool=multi_step_service | workflow=iptu_pagamento | function=consultar_guias"

    Example - Source dinâmico:
        >>> @interceptor(
        ...     source={"source": "mcp", "tool": "equipments"},
        ...     extract_source=lambda args, kwargs, base: {**base, "address": kwargs.get("address", "")[:30]}
        ... )
        ... async def get_nearby_equipments(address: str):
        ...     return await fetch_equipments(address)
        # Flowname: "source=mcp | tool=equipments | address=Av. Presidente Vargas | function=get_nearby_equipments"
    """
    from functools import wraps

    def decorator(func):
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except error_types as e:
                await _handle_error(func, args, kwargs, e)
                raise

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except error_types as e:
                # Para funções sync, executamos o report de forma síncrona via asyncio
                try:
                    loop = asyncio.get_running_loop()
                    task = loop.create_task(_handle_error(func, args, kwargs, e))
                    _track_interceptor_task(task)
                except RuntimeError:
                    # Não há event loop rodando, tenta criar um
                    try:
                        asyncio.run(_handle_error(func, args, kwargs, e))
                    except Exception as send_error:
                        # Log silencioso se falhar ao enviar erro
                        logger.warning(
                            f"Falha ao enviar erro para interceptor: {send_error}"
                        )
                raise

        async def _handle_error(func, args, kwargs, e):
            """Handler interno para processar e reportar erros."""
            # Extrai user_id
            user_id = "unknown"
            if extract_user_id:
                try:
                    user_id = extract_user_id(args, kwargs)
                except Exception:
                    pass
            elif args and hasattr(args[0], "user_id"):
                user_id = args[0].user_id
            elif "user_id" in kwargs:
                user_id = kwargs["user_id"]

            # Constrói source final
            # Começa com o source base e permite modificações via extract_source
            final_source = dict(source)  # Copia para não modificar o original

            if extract_source:
                try:
                    final_source = extract_source(args, kwargs, final_source)
                except Exception:
                    pass

            # Adiciona nome da função ao source
            final_source["function"] = func.__name__

            # Captura traceback
            error_traceback = tb.format_exc()

            # Captura parâmetros da função para input_body
            try:
                sig = inspect.signature(func)
                param_names = list(sig.parameters.keys())
                input_body = {}
                # Mapeia args posicionais
                for i, arg in enumerate(args):
                    if i < len(param_names):
                        param_name = param_names[i]
                        # Evita incluir 'self' ou 'cls'
                        if param_name not in ("self", "cls"):
                            # Serializa apenas tipos simples
                            if isinstance(
                                arg, (str, int, float, bool, list, dict, type(None))
                            ):
                                input_body[param_name] = arg
                            else:
                                input_body[param_name] = str(type(arg).__name__)
                # Adiciona kwargs
                for key, value in kwargs.items():
                    if isinstance(
                        value, (str, int, float, bool, list, dict, type(None))
                    ):
                        input_body[key] = value
                    else:
                        input_body[key] = str(type(value).__name__)
            except Exception:
                input_body = kwargs if kwargs else {}

            # Reporta o erro com novo formato de dicionário
            await send_general_error(
                user_id=user_id,
                source=final_source,
                error_type=type(e).__name__,
                error_message=str(e),
                traceback=error_traceback,
                input_body=input_body if input_body is not None else source,
            )

        # Retorna o wrapper apropriado baseado no tipo da função
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator
