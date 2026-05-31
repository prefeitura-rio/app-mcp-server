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

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Union

import httpx
from loguru import logger
from redis import asyncio as aioredis

from src.config import env

# Lock anti-corrida do refresh (refresh tokens podem rotacionar / ser single-use).
_REFRESH_HTTP_TIMEOUT = 10.0
# TTL do lock > timeout do POST, senão o lock pode expirar no meio da renovação e
# um segundo worker adquirir um lock novo enquanto o primeiro ainda roda.
_REFRESH_LOCK_TTL = 15  # segundos
_LOCK_WAIT = 0.5  # espera entre polls ao perder o lock (segundos)
# Janela de poll cobre o TTL do lock: quem perde o lock espera até o holder
# terminar OU o lock expirar, em vez de desistir cedo e reportar "não autenticado".
_LOCK_MAX_POLLS = int(_REFRESH_LOCK_TTL / _LOCK_WAIT) + 2

# Release atômico do lock (compare-and-delete) — evita apagar o lock de outro
# worker entre o get e o delete se o nosso TTL expirar no meio.
# NOTA: isto é Lua server-side do Redis (`redis.eval`, sandboxed), NÃO `eval()` de
# Python. O script é uma constante fixa; só o lock token vai em ARGV (dado, não
# código) — sem superfície de injeção. Padrão Redlock canônico de release.
_RELEASE_LOCK_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] "
    "then return redis.call('del', KEYS[1]) else return 0 end"
)

# Compare-and-set atômicos do registro do token (Lua server-side do Redis, NÃO
# `eval()` de Python). Mutam o `govbr_token:<phone>` SOMENTE se o refresh_token
# armazenado ainda for o que usamos no POST — fecha a corrida com logout
# (revoke_token) / re-auth (callback do Gateway), que não pegam o refresh lock.
# Scripts fixos; só dados em ARGV — sem superfície de injeção.
_CAS_SETEX_LUA = (
    "local v = redis.call('get', KEYS[1]); "
    "if v == false then return 0 end; "
    "if cjson.decode(v)['refresh_token'] == ARGV[1] then "
    "redis.call('set', KEYS[1], ARGV[3], 'EX', tonumber(ARGV[2])); return 1 "
    "else return 0 end"
)
_CAS_DELETE_LUA = (
    "local v = redis.call('get', KEYS[1]); "
    "if v == false then return 0 end; "
    "if cjson.decode(v)['refresh_token'] == ARGV[1] "
    "then return redis.call('del', KEYS[1]) else return 0 end"
)


async def _release_lock(redis: aioredis.Redis, lock_key: str, lock_token: str) -> None:
    """Libera o lock SOMENTE se ainda formos o dono, de forma atômica (Lua).

    Sem o compare-and-delete atômico, um delete removeria o lock de outro worker
    caso o nosso TTL tenha expirado e ele tenha readquirido — reabrindo a corrida
    de refresh duplo que o lock deveria prevenir.
    """
    try:
        await redis.eval(_RELEASE_LOCK_LUA, 1, lock_key, lock_token)
    except Exception as e:  # release best-effort; o TTL garante liberação eventual
        logger.warning(f"Gov.br refresh: falha ao liberar lock ({e})")


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

    # TTL = max(access, refresh) para manter o refresh_token vivo após a expiração
    # do access — senão a chave é evicção do Redis antes do refresh poder rodar
    # (igual ao storeTokens do Gateway).
    ttl = max(expires_in, refresh_expires_in, 1)
    await redis.setex(
        token_key,
        ttl,
        json.dumps(token_data),
    )

    logger.info(
        f"Gov.br token saved | user={sanitized_phone[:5]}*** | "
        f"context={service_context} | expires_in={expires_in}s"
    )

    await redis.close()
    return True


def _token_view(token_data: Dict[str, Any]) -> Dict[str, Any]:
    """Monta o shape público de get_token a partir do registro bruto do Redis."""
    return {
        "access_token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token"),
        "expires_at": token_data.get("expires_at", 0),
        "refresh_expires_at": token_data.get("refresh_expires_at"),
        "is_valid": True,
        "service_context": token_data.get("service_context", "generic"),
        "user_info": token_data.get("user_info"),
    }


async def _post_refresh(refresh_tok: str) -> Union[Dict[str, Any], bool, None]:
    """POST grant_type=refresh_token ao token endpoint do Identidade Carioca.

    Returns:
        dict  — corpo do token (sucesso, com access_token)
        False — refresh rejeitado definitivamente (400/401 = invalid_grant)
        None  — erro transitório (rede / 5xx / corpo inválido); manter o registro
    """
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_tok,
        "client_id": env.GOVBR_CLIENT_ID,
        "client_secret": env.GOVBR_CLIENT_SECRET,
    }

    async def _do_post():
        async with httpx.AsyncClient(timeout=_REFRESH_HTTP_TIMEOUT) as client:
            return await client.post(
                env.GOVBR_TOKEN_URL,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

    try:
        # Deadline TOTAL de wall-clock: o timeout do httpx é por-fase
        # (connect/read/write/pool), então o POST poderia exceder o TTL do lock.
        # asyncio.wait_for garante que o POST termina dentro da janela do lock.
        resp = await asyncio.wait_for(_do_post(), timeout=_REFRESH_HTTP_TIMEOUT)
    except (httpx.HTTPError, asyncio.TimeoutError) as e:
        logger.warning(f"Gov.br refresh: erro/timeout HTTP ({e})")
        return None

    if resp.status_code in (400, 401):
        # Só `invalid_grant` é falha definitiva do refresh_token do cidadão.
        # Outros 4xx (invalid_client, invalid_request, má config) são problemas
        # de sistema — transitórios; NÃO devem apagar o token do cidadão.
        error_code = ""
        try:
            error_code = (resp.json() or {}).get("error", "")
        except Exception:
            pass
        if error_code == "invalid_grant":
            logger.info("Gov.br refresh: invalid_grant — refresh do cidadão inválido")
            return False
        logger.warning(
            f"Gov.br refresh rejeitado (status={resp.status_code}, error={error_code}) "
            "— tratando como transitório (não apaga o token)"
        )
        return None
    if resp.status_code != 200:
        logger.warning(f"Gov.br refresh: status inesperado {resp.status_code}")
        return None
    try:
        body = resp.json()
    except Exception:
        logger.warning("Gov.br refresh: resposta não-JSON")
        return None
    if not body.get("access_token"):
        logger.warning("Gov.br refresh: resposta sem access_token")
        return None
    return body


async def refresh_token(user_number: str) -> Optional[Dict[str, Any]]:
    """
    Renova o access_token gov.br usando o refresh_token armazenado.

    Chamado quando o access_token expirou mas o refresh_token ainda é válido —
    evita forçar o cidadão a reautenticar a cada expiração do access (tipicamente
    5-60min) enquanto o refresh durar (horas/dias).

    Self-contained no MCP: usa `GOVBR_CLIENT_ID`/`GOVBR_CLIENT_SECRET` +
    `GOVBR_TOKEN_URL`, sem chamar o Gateway. Um lock SETNX por usuário evita
    refresh concorrente. O registro é regravado com TTL = max(access, refresh)
    para manter o refresh_token vivo nas próximas renovações (igual ao
    `storeTokens` do Gateway).

    Returns:
        token dict (mesmo shape de get_token) ou None se não foi possível renovar
        (sem registro, refresh expirado, ou refresh rejeitado).
    """
    try:
        sanitized_phone = _sanitize_phone_number(user_number)
    except ValueError as e:
        logger.warning(f"Invalid phone number in refresh_token: {e}")
        return None

    if not (env.GOVBR_TOKEN_URL and env.GOVBR_CLIENT_ID and env.GOVBR_CLIENT_SECRET):
        logger.warning(
            "Gov.br refresh não configurado (TOKEN_URL/CLIENT_ID/SECRET ausentes)"
        )
        return None

    redis = _get_redis_client()
    token_key = f"govbr_token:{sanitized_phone}"
    lock_key = f"govbr_refresh_lock:{sanitized_phone}"
    try:
        raw = await redis.get(token_key)
        if not raw:
            return None
        try:
            token_data = json.loads(raw)
        except json.JSONDecodeError:
            logger.error(f"Corrupted token data (refresh) for {sanitized_phone[:5]}***")
            return None

        refresh_tok = token_data.get("refresh_token")
        now_ts = int(datetime.now(timezone.utc).timestamp())
        refresh_expires_at = token_data.get("refresh_expires_at", 0)
        if not refresh_tok or now_ts >= refresh_expires_at:
            logger.info(
                f"Refresh token ausente/expirado para {sanitized_phone[:5]}*** — requer reauth"
            )
            return None

        # Lock anti-corrida. Se não adquirir, outro worker está renovando: faz
        # poll até (a) o registro ficar válido — ele renovou; (b) o lock liberar
        # — renovamos nós; ou (c) esgotar a janela do POST (~10s). Não desistir
        # cedo: senão um refresh concorrente normal reportaria "não autenticado".
        lock_token = uuid.uuid4().hex  # valor único p/ release por ownership
        got_lock = await redis.set(lock_key, lock_token, nx=True, ex=_REFRESH_LOCK_TTL)
        if not got_lock:
            for _ in range(_LOCK_MAX_POLLS):
                await asyncio.sleep(_LOCK_WAIT)
                raw2 = await redis.get(token_key)
                if raw2:
                    try:
                        td2 = json.loads(raw2)
                        if int(datetime.now(timezone.utc).timestamp()) < td2.get(
                            "expires_at", 0
                        ):
                            return _token_view(td2)  # outro worker renovou
                    except json.JSONDecodeError:
                        pass
                # o outro pode ter terminado/falhado e liberado o lock
                got_lock = await redis.set(
                    lock_key, lock_token, nx=True, ex=_REFRESH_LOCK_TTL
                )
                if got_lock:
                    break
            if not got_lock:
                return None  # janela esgotada — caller pode tentar de novo

        try:
            # Re-lê SOB o lock: o refresh_token pode ter rotacionado entre a
            # leitura inicial e a aquisição do lock. Postar o token desatualizado
            # falharia (single-use) e apagaria o registro recém-renovado.
            raw_locked = await redis.get(token_key)
            if not raw_locked:
                return None
            try:
                current = json.loads(raw_locked)
            except json.JSONDecodeError:
                return None
            now2 = datetime.now(timezone.utc)
            now2_ts = int(now2.timestamp())
            if now2_ts < current.get("expires_at", 0):
                return _token_view(
                    current
                )  # outro worker já renovou — corrida resolvida
            cur_refresh = current.get("refresh_token")
            if not cur_refresh or now2_ts >= current.get("refresh_expires_at", 0):
                return None

            result = await _post_refresh(cur_refresh)

            if result is False:
                # invalid_grant: apaga ATOMICAMENTE só se o registro ainda for o
                # nosso. Se o CAS retornar 0, um login novo (re-auth concorrente) já
                # substituiu o registro — não apaga E devolve o token novo se válido
                # (senão reportaríamos "não autenticado" com um login válido no Redis).
                deleted = await redis.eval(_CAS_DELETE_LUA, 1, token_key, cur_refresh)
                if deleted == 0:
                    raw_after = await redis.get(token_key)
                    if raw_after:
                        try:
                            after = json.loads(raw_after)
                            if int(datetime.now(timezone.utc).timestamp()) < after.get(
                                "expires_at", 0
                            ):
                                return _token_view(after)
                        except json.JSONDecodeError:
                            pass
                return None
            if not isinstance(result, dict):
                return None  # transitório (None) — mantém registro p/ retry

            expires_in = int(result.get("expires_in", 0))
            refresh_expires_in = int(result.get("refresh_expires_in", 0))

            updated = dict(current)
            updated["access_token"] = result["access_token"]
            # refresh_token pode rotacionar; se não veio, mantém o atual.
            updated["refresh_token"] = result.get("refresh_token") or cur_refresh
            updated["expires_at"] = now2_ts + expires_in
            if refresh_expires_in > 0:
                updated["refresh_expires_at"] = now2_ts + refresh_expires_in
            updated["refreshed_at"] = now2.isoformat()

            # TTL = max(access, refresh) — mantém o refresh_token vivo.
            ttl = max(
                expires_in, int(updated.get("refresh_expires_at", 0)) - now2_ts, 1
            )

            # Grava ATOMICAMENTE só se o refresh_token armazenado ainda for o que
            # usamos — revoke_token()/re-auth do Gateway (que não pegam o lock)
            # podem ter mudado o registro durante o POST.
            wrote = await redis.eval(
                _CAS_SETEX_LUA, 1, token_key, cur_refresh, str(ttl), json.dumps(updated)
            )
            if wrote == 1:
                logger.info(
                    f"Gov.br token renovado | user={sanitized_phone[:5]}*** | "
                    f"expires_in={expires_in}s"
                )
                return _token_view(updated)

            # CAS falhou: logout/re-auth concorrente venceu. Devolve o registro
            # atual se ainda válido (não ressuscita logout, não sobrescreve login).
            raw_after = await redis.get(token_key)
            if raw_after:
                try:
                    after = json.loads(raw_after)
                    if int(datetime.now(timezone.utc).timestamp()) < after.get(
                        "expires_at", 0
                    ):
                        return _token_view(after)
                except json.JSONDecodeError:
                    pass
            return None
        finally:
            await _release_lock(redis, lock_key, lock_token)
    finally:
        await redis.close()


async def get_token(
    user_number: str, allow_refresh: bool = True
) -> Optional[Dict[str, Any]]:
    """
    Recupera token de autenticação gov.br para um usuário.

    Valida automaticamente se o token ainda é válido (não expirado). Se o access
    token expirou mas o refresh_token ainda é válido e `allow_refresh=True`,
    renova de forma transparente (ver `refresh_token`).

    Args:
        user_number: Número do usuário no formato E.164
        allow_refresh: Se True (padrão), tenta renovar via refresh_token quando o
            access expirou. Passe False para leitura pura (sem chamada de rede).

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

    token_json = await redis.get(token_key)
    await redis.close()

    logger.debug(
        f"get_token | user={sanitized_phone[:5]}*** | found={'YES' if token_json else 'NO'}"
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

    if now_timestamp < expires_at:
        return _token_view(token_data)

    # Access token expirou. Se o refresh ainda é válido, renova de forma
    # transparente — o cidadão continua autenticado sem reautenticar.
    refresh_expires_at = token_data.get("refresh_expires_at", 0)
    if (
        allow_refresh
        and token_data.get("refresh_token")
        and now_timestamp < refresh_expires_at
    ):
        logger.info(
            f"Access expirado, tentando refresh | user={sanitized_phone[:5]}***"
        )
        return await refresh_token(user_number)

    logger.info(
        f"Token expired for user {sanitized_phone[:5]}*** | "
        f"expired_at={expires_at} | now={now_timestamp}"
    )
    return None


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
