"""
Gov.br Authentication Tools

Tools MCP para iniciar e gerenciar autenticação OAuth2/PKCE com Gov.br
(via Identidade Carioca) no contexto de conversas WhatsApp.

Fluxo:
1. Tool detecta necessidade de autenticação
2. Chama govbr_auth_init() → gera URL com PKCE
3. Cidadão clica no link, autentica no gov.br
4. Callback retorna ao Gateway → salva tokens no Redis
5. Tool usa get_token() para fazer requests autenticados

Security features:
- PKCE (SHA256) previne code interception
- Rate limiting de tentativas de auth
- State temporário expira em 5min
- Logs de auditoria
- Validação de inputs
"""

import hashlib
import base64
import secrets
import uuid
import json
from typing import Dict, Any
from datetime import datetime, timezone
from urllib.parse import urlencode

from loguru import logger
from redis import asyncio as aioredis

from src.config import env
from src.utils.govbr_token import is_authenticated, get_token_metadata, revoke_token


def _get_redis_client() -> aioredis.Redis:
    """Obtém cliente Redis assíncrono."""
    return aioredis.from_url(env.REDIS_URL, decode_responses=True)


def _generate_pkce_pair() -> tuple[str, str]:
    """
    Gera par code_verifier e code_challenge para PKCE.

    Returns:
        (code_verifier, code_challenge)

    Security:
        - code_verifier: 43-128 caracteres URL-safe random
        - code_challenge: SHA256(verifier), base64url encoded
        - Usa secrets module (cryptographically strong RNG)
    """
    # code_verifier: 32 bytes = 43 chars base64url (sem padding)
    code_verifier = (
        base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("utf-8").rstrip("=")
    )

    # code_challenge: SHA256(code_verifier), base64url encoded
    challenge_bytes = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    code_challenge = (
        base64.urlsafe_b64encode(challenge_bytes).decode("utf-8").rstrip("=")
    )

    return code_verifier, code_challenge


async def _check_rate_limit(user_number: str) -> bool:
    """
    Verifica rate limit de tentativas de autenticação.

    Previne abuso permitindo no máximo 5 tentativas por hora por usuário.

    Args:
        user_number: Número do usuário

    Returns:
        True se dentro do limite, False se excedeu

    Security:
        - Rate limit: 5 tentativas/hora
        - Reset automático após 1 hora (TTL)
    """
    redis = _get_redis_client()
    rate_key = f"govbr_auth_rate:{user_number}"

    # Incrementa contador
    count = await redis.incr(rate_key)

    # Define TTL na primeira tentativa
    if count == 1:
        await redis.expire(rate_key, 3600)  # 1 hora

    await redis.close()

    MAX_ATTEMPTS = 5
    if count > MAX_ATTEMPTS:
        logger.warning(
            f"Rate limit exceeded for govbr auth | user={user_number[:5]}*** | "
            f"attempts={count}"
        )
        return False

    return True


async def govbr_auth_init(
    user_number: str, service_context: str = "consulta_dados"
) -> Dict[str, Any]:
    """
    Inicia fluxo de autenticação gov.br para um usuário.

    Gera URL de autenticação com PKCE e salva estado temporário no Redis.
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

    Security:
        - Rate limit: 5 tentativas/hora
        - State TTL: 5 minutos
        - PKCE SHA256
        - Audit logs
    """
    # Validação básica
    if not user_number or not user_number.startswith("+"):
        return {
            "status": "error",
            "error": "invalid_phone",
            "message": "Número de telefone inválido. Use formato E.164 (+5521999999999)",
        }

    # Verifica configuração
    if not env.GOVBR_CLIENT_ID or not env.GOVBR_REDIRECT_URI:
        logger.error("Gov.br OAuth not configured (missing CLIENT_ID or REDIRECT_URI)")
        return {
            "status": "error",
            "error": "not_configured",
            "message": "Autenticação gov.br não está configurada. Contate o suporte.",
        }

    # Rate limiting
    if not await _check_rate_limit(user_number):
        return {
            "status": "error",
            "error": "rate_limit_exceeded",
            "message": "Você excedeu o número de tentativas de autenticação. "
            "Por favor, aguarde 1 hora e tente novamente.",
            "retry_after": 3600,
        }

    redis = _get_redis_client()

    # 1. Gera identificadores
    auth_id = str(uuid.uuid4())
    code_verifier, code_challenge = _generate_pkce_pair()

    # 2. Salva estado temporário no Redis
    auth_state = {
        "user_number": user_number,
        "code_verifier": code_verifier,  # NUNCA logado, NUNCA exposto ao frontend
        "service_context": service_context,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "state": "pending",
    }

    redis_key = f"govbr_auth:{auth_id}"
    ttl = env.GOVBR_AUTH_STATE_TTL  # default 300 segundos

    await redis.setex(redis_key, ttl, json.dumps(auth_state))

    # 3. Monta URL de autenticação
    params = {
        "client_id": env.GOVBR_CLIENT_ID,
        "redirect_uri": env.GOVBR_REDIRECT_URI,
        "response_type": "code",
        "scope": env.GOVBR_SCOPE,
        "state": auth_id,  # Retornado no callback
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "kc_idp_hint": "govbr",  # Força uso do gov.br no Keycloak
    }

    auth_url = f"{env.GOVBR_AUTH_URL}?{urlencode(params)}"

    await redis.close()

    # Log de auditoria (sem expor verifier ou challenge)
    logger.info(
        f"Gov.br auth initiated | user={user_number[:5]}*** | "
        f"auth_id={auth_id} | context={service_context} | expires_in={ttl}s"
    )

    return {
        "status": "ok",
        "auth_url": auth_url,
        "auth_id": auth_id,
        "expires_in": ttl,
        "message": f"Para continuar com {service_context}, clique no link abaixo para "
        f"autenticar com gov.br. O link expira em {ttl // 60} minutos.",
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
