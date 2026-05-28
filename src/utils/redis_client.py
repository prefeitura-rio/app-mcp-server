"""
Cliente Redis para uso síncrono e assíncrono.

Fornece funções helper para obter clientes Redis configurados com
as variáveis de ambiente do projeto. Suporta tanto operações síncronas
quanto assíncronas.

Uso:
    # Sync (para webhooks, tools síncronos)
    redis = get_redis_client()
    redis.set("key", "value")

    # Async (para operações assíncronas)
    redis = await get_async_redis_client()
    await redis.set("key", "value")
"""

from __future__ import annotations


import redis
from redis import asyncio as aioredis

from src.config import env


def get_redis_client(decode_responses: bool = True) -> redis.Redis:
    """
    Retorna cliente Redis síncrono.

    Usado em contextos síncronos como webhooks, tools MCP síncronos, etc.

    Args:
        decode_responses: Se True, retorna strings ao invés de bytes.
                         Default True para compatibilidade com código existente.

    Returns:
        Cliente Redis configurado.
    """
    return redis.from_url(env.REDIS_URL, decode_responses=decode_responses)


async def get_async_redis_client(
    decode_responses: bool = True,
) -> aioredis.Redis:
    """
    Retorna cliente Redis assíncrono.

    Usado em contextos assíncronos (async/await). Reutiliza padrão do
    govbr_token.py.

    Args:
        decode_responses: Se True, retorna strings ao invés de bytes.

    Returns:
        Cliente Redis async configurado.
    """
    return aioredis.from_url(env.REDIS_URL, decode_responses=decode_responses)
