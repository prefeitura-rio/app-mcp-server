"""
Cache de idempotência best-effort via Redis: roda uma operação no máximo uma vez por
chave determinística e devolve o resultado cacheado nas repetições (retry).

Motivação (#14): `register_sgrc_ticket` é re-chamado pelo gateway/engine em timeout —
sem idempotência, cada retry abriria um chamado SGRC DUPLICADO. O SGRC tem dedup
própria (SGRCDuplicateTicketException) como rede de segurança pro caso concorrente,
mas o cache resolve o retry sequencial (mesmo protocolo de volta, sem chamada
redundante).

Best-effort por design: sem `REDIS_URL`, com URL malformada, ou Redis indisponível/
particionado, é no-op (a operação roda normalmente). Socket timeouts curtos garantem
que um Redis black-holed degrade pra miss rápido em vez de pendurar o chamador.
"""

import json
from typing import Optional

from src.config import env
from src.utils.log import logger

try:
    import redis.asyncio as aioredis
except ImportError:  # pragma: no cover - redis é dependência de runtime
    aioredis = None

# Timeouts curtos: Redis particionado/black-holed degrada pra miss rápido.
_SOCKET_TIMEOUT = 2


async def idempotency_get(key: str) -> Optional[dict]:
    """Resultado cacheado pra `key`, ou None (cache miss / sem Redis / erro)."""
    redis_url = getattr(env, "REDIS_URL", None)
    if not aioredis or not redis_url:
        return None
    client = None
    try:
        # from_url DENTRO do try: URL malformada também cai em best-effort (miss),
        # sem quebrar o register_sgrc_ticket (que awaita isto ANTES da chamada SGRC).
        client = aioredis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=_SOCKET_TIMEOUT,
            socket_timeout=_SOCKET_TIMEOUT,
        )
        raw = await client.get(key)
        return json.loads(raw) if raw else None
    except Exception as e:  # cache é best-effort; nunca derruba a operação
        logger.warning(f"idempotency_get falhou ({e}); seguindo sem cache")
        return None
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass


async def idempotency_set(key: str, value: dict, ttl_seconds: int = 900) -> None:
    """Cacheia `value` (JSON-serializável) sob `key` por `ttl_seconds`. No-op sem Redis."""
    redis_url = getattr(env, "REDIS_URL", None)
    if not aioredis or not redis_url:
        return
    client = None
    try:
        client = aioredis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=_SOCKET_TIMEOUT,
            socket_timeout=_SOCKET_TIMEOUT,
        )
        await client.set(key, json.dumps(value), ex=ttl_seconds)
    except Exception as e:
        logger.warning(f"idempotency_set falhou ({e})")
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass
