"""
Error Interceptor Utility

Utilitário para enviar erros de API e outros erros para o sistema de monitoramento
via endpoint de error interceptor.

Este módulo fornece funções assíncronas para reportar erros de forma não-bloqueante,
garantindo que falhas no envio de erros não afetem o fluxo principal da aplicação.
"""

import json
import traceback as tb
from typing import Any, Dict, Optional
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
    source: str = "eai_agent",
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
        source: Fonte do erro (padrão: "eai_agent")

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

    # Prepara o payload
    payload = {
        "customer_whatsapp_number": customer_whatsapp_number,
        "source": source,
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


async def send_api_error(
    user_id: str,
    service_name: str,
    api_endpoint: str,
    request_body: Any,
    status_code: int,
    error_message: str,
    traceback: Optional[str] = None,
    tool_name: str = "multi_step_service",
) -> bool:
    """
    Wrapper conveniente para enviar erros de API do multi-step service.

    Args:
        user_id: ID do usuário (WhatsApp number)
        service_name: Nome do serviço (ex: "iptu_pagamento")
        api_endpoint: URL do endpoint da API que falhou
        request_body: Body da requisição que causou erro
        status_code: Código HTTP de erro
        error_message: Mensagem de erro
        traceback: Stack trace do erro (opcional)
        tool_name: Nome da tool (padrão: "multi_step_service")

    Returns:
        True se reportado com sucesso, False caso contrário

    Example:
        >>> await send_api_error(
        ...     user_id="5521999999999",
        ...     service_name="iptu_pagamento",
        ...     api_endpoint="https://api.dados.rio/iptu/consultar",
        ...     request_body={"inscricao": "12345678", "ano": 2024},
        ...     status_code=503,
        ...     error_message="Service Unavailable",
        ...     traceback="Traceback..."
        ... )
        True
    """

    flowname = f"{tool_name}({service_name})"

    return await send_error_to_interceptor(
        customer_whatsapp_number=user_id,
        flowname=flowname,
        api_endpoint=api_endpoint,
        input_body=request_body,
        http_status_code=status_code,
        error_message=error_message,
        traceback=traceback,
    )


async def send_general_error(
    user_id: str,
    tool_name: str,
    error_type: str,
    error_message: str,
    context: Optional[Dict[str, Any]] = None,
    traceback: Optional[str] = None,
    http_status_code: int = 0,
) -> bool:
    """
    Wrapper para enviar erros gerais (não relacionados a APIs externas).

    Args:
        user_id: ID do usuário (WhatsApp number)
        tool_name: Nome da ferramenta/serviço
        error_type: Tipo do erro (ex: "ValidationError", "ProcessingError")
        error_message: Mensagem de erro
        context: Contexto adicional do erro (opcional)
        traceback: Stack trace do erro (opcional)
        http_status_code: Código de status (0 para erros internos)

    Returns:
        True se reportado com sucesso, False caso contrário

    Example:
        >>> await send_general_error(
        ...     user_id="5521999999999",
        ...     tool_name="multi_step_service(iptu_pagamento)",
        ...     error_type="ValidationError",
        ...     error_message="Inscrição imobiliária inválida",
        ...     context={"inscricao": "123", "expected_length": 8},
        ...     traceback="Traceback..."
        ... )
        True
    """

    return await send_error_to_interceptor(
        customer_whatsapp_number=user_id,
        flowname=tool_name,
        api_endpoint=f"internal://{error_type}",
        input_body=context or {},
        http_status_code=http_status_code,
        error_message=error_message,
        traceback=traceback,
    )


# Decorator para capturar erros automaticamente (opcional)
def intercept_errors(service_name: str, tool_name: str = "multi_step_service"):
    """
    Decorator para capturar e reportar erros automaticamente.

    Args:
        service_name: Nome do serviço
        tool_name: Nome da tool

    Example:
        >>> @intercept_errors("iptu_pagamento")
        ... async def fetch_data(user_id: str):
        ...     # código que pode gerar erro
        ...     pass
    """

    def decorator(func):
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                # Tenta extrair user_id dos argumentos
                user_id = "unknown"
                if args and hasattr(args[0], "user_id"):
                    user_id = args[0].user_id
                elif "user_id" in kwargs:
                    user_id = kwargs["user_id"]

                # Captura traceback
                error_traceback = tb.format_exc()

                # Reporta o erro
                await send_general_error(
                    user_id=user_id,
                    tool_name=f"{tool_name}({service_name})",
                    error_type=type(e).__name__,
                    error_message=str(e),
                    context={"function": func.__name__, "args": str(args)[:200]},
                    traceback=error_traceback,
                )

                # Re-levanta a exceção
                raise

        return wrapper

    return decorator
