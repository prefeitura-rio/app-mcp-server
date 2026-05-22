"""
Gov.br Authentication Tools

Tools MCP para iniciar e gerenciar autenticação OAuth2/PKCE com Gov.br
(via Identidade Carioca) no contexto de conversas WhatsApp.

Fluxo:
1. Tool detecta necessidade de autenticação
2. Chama govbr_auth_init() → chama Gateway endpoint /initiate
3. Gateway gera URL com PKCE e retorna
4. Cidadão clica no link, autentica no gov.br
5. Callback retorna ao Gateway → salva tokens no Redis
6. Tool usa get_token() para fazer requests autenticados

Security features:
- Gateway aplica autenticação Bearer token
- Gateway aplica rate limiting (5 tentativas/hora)
- Gateway aplica validação de telefone E.164
- Gateway gera PKCE (SHA256) seguro
- State temporário expira em 5min no Gateway
- Logs de auditoria
"""

from typing import Dict, Any

import httpx
from loguru import logger

from src.config import env
from src.utils.govbr_token import is_authenticated, get_token_metadata, revoke_token


async def govbr_auth_init(
    user_number: str, service_context: str = "consulta_dados"
) -> Dict[str, Any]:
    """
    Inicia fluxo de autenticação gov.br para um usuário.

    Chama o Gateway endpoint que gera URL de autenticação com PKCE
    e salva estado temporário no Redis.
    O cidadão deve clicar na URL para autenticar.

    Args:
        user_number: Número do usuário no formato E.164 (ex: +5521999999999)
        service_context: Contexto do serviço que requer auth (ex: iptu, multas, consultas)

    Returns:
        {
            "status": "ok",
            "auth_url": "https://identidade.prefeitura.rio/auth?...",
            "auth_id": "uuid",
            "expires_in": 300,
            "message": "Clique no link para autenticar com gov.br"
        }
        ou
        {
            "status": "error",
            "error": "rate_limit_exceeded",
            "message": "Muitas tentativas. Aguarde 1 hora."
        }

    Security (aplicada pelo Gateway):
        - Autenticação Bearer token
        - Rate limit: 5 tentativas/hora
        - Validação E.164 do telefone
        - PKCE SHA256
        - State TTL: 5 minutos
        - Audit logs
    """
    # Validação básica local
    if not user_number:
        return {
            "status": "error",
            "error": "invalid_phone",
            "message": "Número de telefone não informado.",
        }

    # Remove "+" se presente (Gateway aceita com ou sem)
    phone_number = user_number.lstrip("+")

    try:
        # Chama Gateway endpoint /initiate
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{env.EAI_GATEWAY_API_URL}api/v1/auth/govbr/initiate",
                headers={
                    "Authorization": f"Bearer {env.EAI_GATEWAY_API_TOKEN}",
                    "Content-Type": "application/json",
                },
                json={
                    "user_number": phone_number,
                    "service_context": service_context,
                },
            )

            # Log de auditoria
            logger.info(
                f"Gov.br auth initiate called | user={user_number[:5]}*** | "
                f"context={service_context} | status={response.status_code}"
            )

            # Trata erros HTTP
            if response.status_code == 401:
                logger.error(
                    "Gateway authentication failed - check EAI_GATEWAY_API_TOKEN"
                )
                return {
                    "status": "error",
                    "error": "gateway_auth_failed",
                    "message": "Erro de autenticação com o Gateway. Contate o suporte.",
                }

            if response.status_code == 429:
                return {
                    "status": "error",
                    "error": "rate_limit_exceeded",
                    "message": "Você excedeu o número de tentativas de autenticação. "
                    "Por favor, aguarde 1 hora e tente novamente.",
                    "retry_after": 3600,
                }

            if response.status_code == 400:
                error_data = response.json()
                return {
                    "status": "error",
                    "error": error_data.get("error", "invalid_request"),
                    "message": error_data.get("message", "Requisição inválida."),
                }

            if response.status_code != 200:
                logger.error(
                    f"Gateway returned unexpected status | "
                    f"status={response.status_code} | body={response.text[:200]}"
                )
                return {
                    "status": "error",
                    "error": "gateway_error",
                    "message": "Erro ao iniciar autenticação. Tente novamente.",
                }

            # Sucesso - adapta resposta do Gateway para formato esperado
            gateway_response = response.json()

            return {
                "status": "ok",
                "auth_url": gateway_response["auth_url"],
                "auth_id": gateway_response[
                    "state"
                ],  # Gateway usa "state" em vez de "auth_id"
                "expires_in": gateway_response["expires_in"],
                "message": f"Para continuar com {service_context}, clique no link abaixo para "
                f"autenticar com gov.br. O link expira em {gateway_response['expires_in'] // 60} minutos.\n\n"
                f"Quando você terminar a autenticação, me avise!",
            }

    except httpx.TimeoutException:
        logger.error("Gateway request timeout")
        return {
            "status": "error",
            "error": "timeout",
            "message": "Timeout ao contactar Gateway. Tente novamente.",
        }
    except httpx.RequestError as e:
        logger.error(f"Gateway request error | error={e}")
        return {
            "status": "error",
            "error": "connection_error",
            "message": "Erro de conexão com Gateway. Tente novamente.",
        }
    except Exception as e:
        logger.error(f"Unexpected error in govbr_auth_init | error={e}")
        return {
            "status": "error",
            "error": "unknown_error",
            "message": "Erro inesperado. Contate o suporte.",
        }


async def govbr_auth_status(user_number: str) -> Dict[str, Any]:
    """
    Verifica status de autenticação de um usuário.

    Útil para verificar se usuário já está autenticado antes de
    solicitar nova autenticação.

    Args:
        user_number: Número do usuário no formato E.164

    Returns:
        {
            "status": "authenticated",
            "is_authenticated": true,
            "token_valid": true,
            "expires_in": 250,  # segundos restantes
            "service_context": "iptu",
            "user_info": {...}
        }
        ou
        {
            "status": "not_authenticated",
            "is_authenticated": false,
            "message": "Usuário não autenticado"
        }
    """
    is_auth = await is_authenticated(user_number)

    if not is_auth:
        return {
            "status": "not_authenticated",
            "is_authenticated": False,
            "message": "Usuário não possui autenticação gov.br ativa.",
        }

    # Busca metadados do token
    metadata = await get_token_metadata(user_number)

    if not metadata:
        return {
            "status": "not_authenticated",
            "is_authenticated": False,
            "message": "Token expirado ou inválido.",
        }

    return {
        "status": "authenticated",
        "is_authenticated": True,
        "token_valid": metadata["is_valid"],
        "expires_in": metadata["time_remaining"],
        "service_context": metadata.get("service_context"),
        "user_info": metadata.get("user_info"),
        "message": f"Autenticado. Token válido por mais {metadata['time_remaining']}s.",
    }


async def govbr_logout(user_number: str) -> Dict[str, Any]:
    """
    Faz logout do usuário, revogando token de autenticação.

    Remove token localmente (Redis). Nota: não revoga no provider
    (Identidade Carioca) - o token continuará válido lá até expirar.

    Args:
        user_number: Número do usuário no formato E.164

    Returns:
        {
            "status": "ok",
            "message": "Logout realizado com sucesso"
        }

    Security:
        - Operação idempotente
        - Log de auditoria
    """
    success = await revoke_token(user_number)

    if success:
        logger.info(f"Gov.br logout | user={user_number[:5]}***")
        return {
            "status": "ok",
            "message": "Você foi desconectado com sucesso. "
            "Será necessário autenticar novamente para acessar serviços restritos.",
        }
    else:
        return {
            "status": "error",
            "error": "logout_failed",
            "message": "Erro ao fazer logout. Tente novamente.",
        }


# Exporta tools para registro no MCP
__all__ = [
    "govbr_auth_init",
    "govbr_auth_status",
    "govbr_logout",
]
