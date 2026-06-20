"""
Lock distribuído best-effort via Redis (padrão Redlock simplificado), reaproveitando
o mesmo esquema de `src/utils/govbr_token.py` (SETNX com token + release atômico via
Lua compare-and-delete).

Uso:

    async with redis_lock(f"service_state_lock:{user_id}", redis_url=backend_url):
        # seção crítica (read-modify-write serializado por chave)
        ...

Best-effort por design: se não houver Redis configurado/disponível (ou URL malformada),
o context manager NÃO bloqueia o fluxo (cede sem lock). A correção de concorrência só
vale quando há Redis (produção); em dev/JSON puro o lock é no-op. O TTL garante
liberação eventual mesmo se o processo morrer segurando o lock. Socket timeouts curtos
evitam pendurar a seção crítica quando o Redis está particionado/black-holed.

`redis_url`: passe a MESMA URL do backend que guarda os dados protegidos (StateManager)
pra o lock viver no mesmo Redis. None/vazio → no-op (sem lock). NÃO há fallback pra
env.REDIS_URL — assim o modo JSON (sem store compartilhado) não locka à toa.
"""

import asyncio
import hashlib
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from src.utils.log import logger

try:
    import redis.asyncio as aioredis
except ImportError:  # pragma: no cover - redis é dependência de runtime
    aioredis = None

# Compare-and-delete atômico: só libera o lock se ainda formos o dono (token bate).
# Script fixo; só o token vai em ARGV (dado, não código) — sem superfície de injeção.
_RELEASE_LOCK_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] "
    "then return redis.call('del', KEYS[1]) else return 0 end"
)

_SOCKET_TIMEOUT = 2


@asynccontextmanager
async def redis_lock(
    lock_key: str,
    ttl_seconds: int = 30,
    wait_seconds: float = 5.0,
    poll_interval: float = 0.05,
    redis_url: Optional[str] = None,
) -> AsyncIterator[bool]:
    """Adquire um lock distribuído por `lock_key` enquanto durar o bloco `async with`.

    Cede `True` se o lock foi adquirido, `False` se não (sem Redis, URL ruim, timeout
    de espera, ou erro) — em todos os casos o bloco do chamador EXECUTA (best-effort,
    nunca derruba o fluxo por causa do lock). Quem precisa de garantia forte deve
    checar o valor cedido.
    """
    # redis_url None/vazio → no-op (sem lock): o CHAMADOR resolve a url (o StateManager
    # passa a url do backend em REDIS/BOTH e None em JSON local). SEM fallback pra
    # env.REDIS_URL aqui — senão o modo JSON (que costuma ter REDIS_URL setado no
    # ambiente) lockaria à toa e pagaria timeout de Redis a cada save (#14 P2 codex).
    if not aioredis or not redis_url:
        yield False
        return

    client = None
    token = uuid.uuid4().hex
    acquired = False
    # Não logar o lock_key cru: ele carrega o user_id (telefone do cidadão) — LGPD.
    # Loga só um hash curto pra correlação (#14 P2 codex).
    _key_log = "sha256:" + hashlib.sha256(lock_key.encode("utf-8")).hexdigest()[:12]
    try:
        # from_url + aquisição DENTRO do try: URL malformada / Redis indisponível
        # degradam pra best-effort (sem lock) em vez de derrubar a seção crítica.
        try:
            client = aioredis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=_SOCKET_TIMEOUT,
                socket_timeout=_SOCKET_TIMEOUT,
            )
            deadline = time.monotonic() + wait_seconds
            while True:
                acquired = bool(
                    await client.set(lock_key, token, nx=True, ex=ttl_seconds)
                )
                if acquired or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(poll_interval)
        except Exception as e:  # URL ruim / Redis fora → segue sem lock
            logger.warning(
                f"redis_lock: erro ao adquirir {_key_log} ({e}); seguindo sem lock"
            )
            acquired = False

        if not acquired:
            logger.warning(
                f"redis_lock: não adquiriu {_key_log} em {wait_seconds}s; "
                "executando best-effort (risco residual de corrida)"
            )
        yield acquired
    finally:
        if acquired and client is not None:
            try:
                await client.eval(_RELEASE_LOCK_LUA, 1, lock_key, token)
            except Exception as e:  # release best-effort; o TTL libera depois
                logger.warning(f"redis_lock: falha ao liberar {_key_log} ({e})")
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass
