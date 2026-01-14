"""
Error Interceptor Utility

Utilitário para enviar erros de API e outros erros para o sistema de monitoramento
via endpoint de error interceptor.

Este módulo fornece funções assíncronas para reportar erros de forma não-bloqueante,
garantindo que falhas no envio de erros não afetem o fluxo principal da aplicação.
"""

import inspect
import json
import traceback as tb
from typing import Any, Callable, Dict, Optional
import httpx
from loguru import logger

from src.config import env


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
        input_body_str = json.dumps(input_body, ensure_ascii=False)
    elif input_body is None:
        input_body_str = ""
    else:
        input_body_str = str(input_body)

    # Prepara error_response como JSON string contendo error_message e traceback
    error_response_data = {
        "error_message": error_message,
        "traceback": traceback or ""
    }
    error_response_str = json.dumps(error_response_data, ensure_ascii=False)

    # Serializa source como JSON string se for dicionário
    if source is not None:
        source_str = json.dumps(source, indent=2, ensure_ascii=False)
    else:
        source_str = "mcp"

    # Prepara o payload
    payload = {
        "customer_whatsapp_number": customer_whatsapp_number,
        "source": source_str,
        "flowname": flowname,
        "api_endpoint": api_endpoint,
        "input_body": input_body_str,
        "http_status_code": http_status_code,
        "error_response": error_response_str,
    }

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
        # Flowname será enviado como JSON formatado do dicionário source

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

    # Cria flowname simples a partir do source
    flowname = source.get("tool", "unknown")
    if "workflow" in source:
        flowname = f"{flowname}({source['workflow']})"
    if "function" in source:
        flowname = f"{flowname}.{source['function']}"

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
        # Flowname será enviado como JSON formatado do dicionário source

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

    # Cria flowname simples a partir do source
    flowname = source.get("tool", "unknown")
    if "workflow" in source:
        flowname = f"{flowname}({source['workflow']})"
    if "function" in source:
        flowname = f"{flowname}.{source['function']}"

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
    import asyncio

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
                    loop.create_task(_handle_error(func, args, kwargs, e))
                except RuntimeError:
                    # Não há event loop rodando, tenta criar um
                    asyncio.run(_handle_error(func, args, kwargs, e))
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
                        if param_name not in ('self', 'cls'):
                            # Serializa apenas tipos simples
                            if isinstance(arg, (str, int, float, bool, list, dict, type(None))):
                                input_body[param_name] = arg
                            else:
                                input_body[param_name] = str(type(arg).__name__)
                # Adiciona kwargs
                for key, value in kwargs.items():
                    if isinstance(value, (str, int, float, bool, list, dict, type(None))):
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
                input_body=input_body,
            )

        # Retorna o wrapper apropriado baseado no tipo da função
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator
