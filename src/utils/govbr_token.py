"""
Gov.br Token Management Utilities

Gerencia tokens de autenticação OAuth2/PKCE do Gov.br (via Identidade Carioca).
Tokens são armazenados no Redis com TTL baseado no expires_in retornado pelo provider.

Security considerations:
- Tokens são armazenados com TTL apropriado
- Validação de expiração antes de retornar token
- Logs nunca expõem valores de tokens (apenas metadados)
- User numbers são sanitizados antes de usar como chave Redis
"""

import json
import re
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from loguru import logger
from redis import asyncio as aioredis

from src.config import env


def _sanitize_phone_number(phone: str) -> str:
    """
    Sanitiza número de telefone para uso seguro como chave Redis.

    Remove caracteres não-numéricos e valida formato básico.
    Previne injection de caracteres especiais nas chaves Redis.

    Args:
        phone: Número no formato E.164 (ex: +5521999999999)

    Returns:
        Número sanitizado (apenas dígitos)

    Raises:
        ValueError: Se número inválido
    """
    # Remove tudo exceto dígitos
    sanitized = re.sub(r"[^\d]", "", phone)

    # Valida: deve ter entre 10 e 15 dígitos (padrão E.164)
    if not 10 <= len(sanitized) <= 15:
        raise ValueError(f"Invalid phone number format: {phone}")

    return sanitized


def _get_redis_client() -> aioredis.Redis:
    """Obtém cliente Redis assíncrono."""
    return aioredis.from_url(env.REDIS_URL, decode_responses=True)


async def save_token(
    user_number: str,
    access_token: str,
    refresh_token: str,
    expires_in: int,
    refresh_expires_in: int,
    service_context: str = "generic",
    user_info: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Salva token de autenticação gov.br no Redis.

    ATENÇÃO: Esta função deve ser chamada APENAS pelo callback handler do Gateway,
    nunca diretamente pelas tools MCP.

    Args:
        user_number: Número do usuário no formato E.164
        access_token: Access token JWT do Identidade Carioca
        refresh_token: Refresh token para renovação
        expires_in: TTL do access token em segundos (tipicamente 300-3600)
        refresh_expires_in: TTL do refresh token em segundos
        service_context: Contexto do serviço que solicitou auth (ex: iptu, multas)
        user_info: Informações do usuário (CPF, nome) - OPCIONAL

    Returns:
        True se salvo com sucesso

    Security:
        - Tokens nunca são logados
        - TTL é respeitado automaticamente pelo Redis
        - Chaves são sanitizadas contra injection
    """
    sanitized_phone = _sanitize_phone_number(user_number)

    redis = _get_redis_client()

    # Calcula timestamps de expiração
    now = datetime.now(timezone.utc)
    expires_at = int(now.timestamp()) + expires_in
    refresh_expires_at = int(now.timestamp()) + refresh_expires_in

    token_data = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
        "refresh_expires_at": refresh_expires_at,
        "service_context": service_context,
        "created_at": now.isoformat(),
    }

    # Adiciona user_info se fornecido (sanitizado)
    if user_info:
        # Remove campos sensíveis que não devemos armazenar
        safe_user_info = {
            k: v
            for k, v in user_info.items()
            if k in {"nome", "cpf", "email"}  # whitelist explícita
        }
        token_data["user_info"] = safe_user_info

    token_key = f"govbr_token:{sanitized_phone}"

    # Serializa e salva com TTL do access token
    await redis.setex(
        token_key,
        expires_in,  # TTL = expires_in do access token
        json.dumps(token_data),
    )

    logger.info(
        f"Gov.br token saved | user={sanitized_phone[:5]}*** | "
        f"context={service_context} | expires_in={expires_in}s"
    )

    await redis.close()
    return True


async def get_token(user_number: str) -> Optional[Dict[str, Any]]:
    """
    Recupera token de autenticação gov.br para um usuário.

    Valida automaticamente se o token ainda é válido (não expirado).

    Args:
        user_number: Número do usuário no formato E.164

    Returns:
        {
            "access_token": "...",
            "refresh_token": "...",
            "expires_at": timestamp,
            "refresh_expires_at": timestamp,
            "is_valid": bool,
            "service_context": str,
            "user_info": {...}  # opcional
        }
        ou None se não autenticado ou token expirado

    Security:
        - Retorna None se token expirado (mesmo que ainda no Redis)
        - Logs nunca expõem o valor do token
    """
    try:
        sanitized_phone = _sanitize_phone_number(user_number)
    except ValueError as e:
        logger.warning(f"Invalid phone number in get_token: {e}")
        return None

    redis = _get_redis_client()
    token_key = f"govbr_token:{sanitized_phone}"

    logger.info(
        f"[DEBUG] get_token | user={user_number} | sanitized={sanitized_phone} | key={token_key}"
    )

    token_json = await redis.get(token_key)
    await redis.close()

    logger.info(
        f"[DEBUG] Redis response | key={token_key} | found={'YES' if token_json else 'NO'}"
    )

    if not token_json:
        return None

    try:
        token_data = json.loads(token_json)
    except json.JSONDecodeError:
        logger.error(f"Corrupted token data for user {sanitized_phone[:5]}***")
        return None

    # Valida expiração
    expires_at = token_data.get("expires_at", 0)
    now_timestamp = int(datetime.now(timezone.utc).timestamp())
    is_valid = now_timestamp < expires_at

    if not is_valid:
        logger.info(
            f"Token expired for user {sanitized_phone[:5]}*** | "
            f"expired_at={expires_at} | now={now_timestamp}"
        )
        return None

    return {
        "access_token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token"),
        "expires_at": expires_at,
        "refresh_expires_at": token_data.get("refresh_expires_at"),
        "is_valid": is_valid,
        "service_context": token_data.get("service_context", "generic"),
        "user_info": token_data.get("user_info"),
    }


async def is_authenticated(user_number: str) -> bool:
    """
    Verifica se usuário possui autenticação gov.br válida.

    Args:
        user_number: Número do usuário no formato E.164

    Returns:
        True se autenticado com token válido, False caso contrário
    """
    token_data = await get_token(user_number)
    return token_data is not None and token_data.get("is_valid", False)


async def revoke_token(user_number: str) -> bool:
    """
    Revoga token de autenticação (logout).

    Remove token do Redis, efetivamente desautenticando o usuário.
    Nota: Não revoga o token no provider (Identidade Carioca), apenas
    localmente. O token continuará válido no provider até expirar.

    Args:
        user_number: Número do usuário no formato E.164

    Returns:
        True se revogado com sucesso (ou se não existia)

    Security:
        - Operação idempotente (safe para chamar múltiplas vezes)
        - Logs de auditoria
    """
    try:
        sanitized_phone = _sanitize_phone_number(user_number)
    except ValueError as e:
        logger.warning(f"Invalid phone number in revoke_token: {e}")
        return False

    redis = _get_redis_client()
    token_key = f"govbr_token:{sanitized_phone}"

    deleted = await redis.delete(token_key)
    await redis.close()

    if deleted > 0:
        logger.info(f"Gov.br token revoked | user={sanitized_phone[:5]}***")
    else:
        logger.debug(f"No token to revoke for user {sanitized_phone[:5]}***")

    return True


async def get_token_metadata(user_number: str) -> Optional[Dict[str, Any]]:
    """
    Retorna metadados do token SEM expor o access_token.

    Útil para debugging e auditoria sem risco de leak de tokens.

    Args:
        user_number: Número do usuário no formato E.164

    Returns:
        {
            "is_valid": bool,
            "expires_at": timestamp,
            "service_context": str,
            "time_remaining": int  # segundos até expirar
        }
        ou None se não autenticado
    """
    token_data = await get_token(user_number)

    if not token_data:
        return None

    now_timestamp = int(datetime.now(timezone.utc).timestamp())
    expires_at = token_data["expires_at"]
    time_remaining = max(0, expires_at - now_timestamp)

    return {
        "is_valid": token_data["is_valid"],
        "expires_at": expires_at,
        "refresh_expires_at": token_data.get("refresh_expires_at"),
        "service_context": token_data.get("service_context"),
        "time_remaining": time_remaining,
        "user_info": token_data.get("user_info"),  # já sanitizado
    }
