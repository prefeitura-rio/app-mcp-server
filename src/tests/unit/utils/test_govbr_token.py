"""Tests pra src/utils/govbr_token.py — foco no refresh de token gov.br (#7).

httpx é evitado patchando `_post_refresh`; o Redis é um fake em memória. Sem rede.
"""

import json
from datetime import datetime, timezone

import pytest

from src.utils import govbr_token


class FakeRedis:
    """Redis async em memória com semântica de SET NX + setex (captura TTL)."""

    def __init__(self, store=None):
        self.store = dict(store or {})
        self.setex_calls = []
        self.deleted = []

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None  # lock não adquirido
        self.store[key] = value
        return True

    async def setex(self, key, ttl, value):
        self.setex_calls.append((key, ttl, value))
        self.store[key] = value
        return True

    async def delete(self, key):
        existed = key in self.store
        self.store.pop(key, None)
        self.deleted.append(key)
        return 1 if existed else 0

    async def eval(self, script, numkeys, *args):
        """Emula os 3 scripts Lua (release lock + CAS setex/delete) por igualdade
        do texto do script (basta pros testes)."""
        key = args[0]
        if script == govbr_token._RELEASE_LOCK_LUA:
            # compare-and-delete por valor do lock
            if self.store.get(key) == args[1]:
                existed = key in self.store
                self.store.pop(key, None)
                self.deleted.append(key)
                return 1 if existed else 0
            return 0
        if script == govbr_token._CAS_DELETE_LUA:
            v = self.store.get(key)
            if v and json.loads(v).get("refresh_token") == args[1]:
                self.store.pop(key, None)
                self.deleted.append(key)
                return 1
            return 0
        if script == govbr_token._CAS_SETEX_LUA:
            v = self.store.get(key)
            if v and json.loads(v).get("refresh_token") == args[1]:
                ttl, value = int(args[2]), args[3]
                self.setex_calls.append((key, ttl, value))
                self.store[key] = value
                return 1
            return 0
        return 0

    async def close(self):
        pass


def _now():
    return int(datetime.now(timezone.utc).timestamp())


def _record(access_offset, refresh_offset, refresh_token="rt-old"):
    """Registro bruto no formato gravado pelo Gateway."""
    now = _now()
    return {
        "access_token": "at-old",
        "refresh_token": refresh_token,
        "expires_at": now + access_offset,
        "refresh_expires_at": now + refresh_offset,
        "service_context": "iptu",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "user_info": {"cpf": "12345678900"},
    }


@pytest.fixture
def fake_redis(monkeypatch):
    fr = FakeRedis()
    monkeypatch.setattr(govbr_token, "_get_redis_client", lambda: fr)
    # credenciais presentes (senão refresh aborta cedo)
    monkeypatch.setattr(
        govbr_token.env, "GOVBR_TOKEN_URL", "https://idrio/token", raising=False
    )
    monkeypatch.setattr(govbr_token.env, "GOVBR_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(govbr_token.env, "GOVBR_CLIENT_SECRET", "secret", raising=False)
    return fr


def test_token_view_shape():
    rec = _record(300, 3600)
    view = govbr_token._token_view(rec)
    assert view["access_token"] == "at-old"
    assert view["is_valid"] is True
    assert view["service_context"] == "iptu"
    assert view["user_info"] == {"cpf": "12345678900"}


async def test_refresh_happy_path_updates_redis_with_long_ttl(fake_redis, monkeypatch):
    # access expirou (-10), refresh ainda válido (+3600)
    fake_redis.store["govbr_token:5521999999999"] = json.dumps(_record(-10, 3600))

    async def fake_post(_rt):
        return {
            "access_token": "at-new",
            "refresh_token": "rt-new",
            "expires_in": 300,
            "refresh_expires_in": 7200,
        }

    monkeypatch.setattr(govbr_token, "_post_refresh", fake_post)

    out = await govbr_token.refresh_token("+5521999999999")

    assert out is not None
    assert out["access_token"] == "at-new"
    assert out["is_valid"] is True
    # regravou com TTL = max(access=300, refresh=7200) → 7200, não o TTL curto
    assert fake_redis.setex_calls, "esperava setex"
    _key, ttl, value = fake_redis.setex_calls[-1]
    assert ttl == 7200
    stored = json.loads(value)
    assert stored["access_token"] == "at-new"
    assert stored["refresh_token"] == "rt-new"  # rotação preservada
    assert "refreshed_at" in stored


async def test_refresh_keeps_old_refresh_token_if_not_rotated(fake_redis, monkeypatch):
    fake_redis.store["govbr_token:5521999999999"] = json.dumps(_record(-10, 3600))

    async def fake_post(_rt):
        return {"access_token": "at-new", "expires_in": 300, "refresh_expires_in": 0}

    monkeypatch.setattr(govbr_token, "_post_refresh", fake_post)
    out = await govbr_token.refresh_token("+5521999999999")
    assert out is not None
    stored = json.loads(fake_redis.setex_calls[-1][2])
    assert stored["refresh_token"] == "rt-old"  # mantém o atual


async def test_refresh_returns_none_when_refresh_expired(fake_redis, monkeypatch):
    # refresh também expirou
    fake_redis.store["govbr_token:5521999999999"] = json.dumps(_record(-100, -10))
    called = {"posted": False}

    async def fake_post(_rt):
        called["posted"] = True
        return {"access_token": "x"}

    monkeypatch.setattr(govbr_token, "_post_refresh", fake_post)
    out = await govbr_token.refresh_token("+5521999999999")
    assert out is None
    assert called["posted"] is False  # nem tentou o POST


async def test_refresh_invalid_grant_deletes_record(fake_redis, monkeypatch):
    key = "govbr_token:5521999999999"
    fake_redis.store[key] = json.dumps(_record(-10, 3600))

    async def fake_post(_rt):
        return False  # invalid_grant

    monkeypatch.setattr(govbr_token, "_post_refresh", fake_post)
    out = await govbr_token.refresh_token("+5521999999999")
    assert out is None
    assert key in fake_redis.deleted  # registro limpo → força reauth


async def test_refresh_transient_error_keeps_record(fake_redis, monkeypatch):
    key = "govbr_token:5521999999999"
    fake_redis.store[key] = json.dumps(_record(-10, 3600))

    async def fake_post(_rt):
        return None  # transitório

    monkeypatch.setattr(govbr_token, "_post_refresh", fake_post)
    out = await govbr_token.refresh_token("+5521999999999")
    assert out is None
    assert key not in fake_redis.deleted  # mantém p/ retry
    assert key in fake_redis.store


async def test_refresh_no_record_returns_none(fake_redis, monkeypatch):
    monkeypatch.setattr(govbr_token, "_post_refresh", lambda _rt: None)
    out = await govbr_token.refresh_token("+5521999999999")
    assert out is None


async def test_get_token_valid_returns_without_refresh(fake_redis, monkeypatch):
    fake_redis.store["govbr_token:5521999999999"] = json.dumps(_record(300, 3600))
    called = {"refreshed": False}

    async def fake_refresh(_u):
        called["refreshed"] = True
        return None

    monkeypatch.setattr(govbr_token, "refresh_token", fake_refresh)
    out = await govbr_token.get_token("+5521999999999")
    assert out is not None
    assert out["access_token"] == "at-old"
    assert called["refreshed"] is False


async def test_get_token_expired_triggers_refresh(fake_redis, monkeypatch):
    fake_redis.store["govbr_token:5521999999999"] = json.dumps(_record(-10, 3600))
    sentinel = {"access_token": "at-new", "is_valid": True}

    async def fake_refresh(user_number):
        assert user_number == "+5521999999999"
        return sentinel

    monkeypatch.setattr(govbr_token, "refresh_token", fake_refresh)
    out = await govbr_token.get_token("+5521999999999")
    assert out is sentinel


async def test_get_token_allow_refresh_false_skips(fake_redis, monkeypatch):
    fake_redis.store["govbr_token:5521999999999"] = json.dumps(_record(-10, 3600))

    async def fake_refresh(_u):
        raise AssertionError("não deveria renovar com allow_refresh=False")

    monkeypatch.setattr(govbr_token, "refresh_token", fake_refresh)
    out = await govbr_token.get_token("+5521999999999", allow_refresh=False)
    assert out is None


# --- _post_refresh: distingue invalid_grant (deleta) de erro de sistema (mantém) ---


class _FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def post(self, url, data=None, headers=None):
        return self._resp


def _patch_httpx(monkeypatch, resp):
    monkeypatch.setattr(govbr_token.env, "GOVBR_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(govbr_token.env, "GOVBR_CLIENT_SECRET", "secret", raising=False)
    monkeypatch.setattr(
        govbr_token.env, "GOVBR_TOKEN_URL", "https://idrio/token", raising=False
    )
    monkeypatch.setattr(
        govbr_token.httpx, "AsyncClient", lambda *a, **k: _FakeClient(resp)
    )


async def test_post_refresh_success(monkeypatch):
    _patch_httpx(monkeypatch, _FakeResp(200, {"access_token": "at", "expires_in": 300}))
    out = await govbr_token._post_refresh("rt")
    assert isinstance(out, dict) and out["access_token"] == "at"


async def test_post_refresh_invalid_grant_is_false(monkeypatch):
    _patch_httpx(monkeypatch, _FakeResp(400, {"error": "invalid_grant"}))
    out = await govbr_token._post_refresh("rt")
    assert out is False  # definitivo → caller deleta


async def test_post_refresh_invalid_client_is_transient(monkeypatch):
    # 400 mas error != invalid_grant → problema de sistema → NÃO deletar
    _patch_httpx(monkeypatch, _FakeResp(400, {"error": "invalid_client"}))
    out = await govbr_token._post_refresh("rt")
    assert out is None


async def test_post_refresh_5xx_is_transient(monkeypatch):
    _patch_httpx(monkeypatch, _FakeResp(503, {}))
    out = await govbr_token._post_refresh("rt")
    assert out is None


async def test_refresh_lock_contended_returns_concurrent_result(
    fake_redis, monkeypatch
):
    """P1: se o lock está ocupado e outro worker já renovou, retorna o token
    válido em vez de None (não derruba a autenticação)."""
    monkeypatch.setattr(govbr_token, "_LOCK_WAIT", 0.0)  # sem espera real no teste
    key = "govbr_token:5521999999999"
    # lock ocupado por outro worker
    fake_redis.store["govbr_refresh_lock:5521999999999"] = "1"
    # registro JÁ renovado por esse outro worker (access válido)
    fake_redis.store[key] = json.dumps(_record(300, 7200, refresh_token="rt-new"))

    async def fake_post(_rt):
        raise AssertionError("não deveria postar com lock ocupado")

    monkeypatch.setattr(govbr_token, "_post_refresh", fake_post)
    out = await govbr_token.refresh_token("+5521999999999")
    assert out is not None and out["is_valid"] is True
    assert out["access_token"] == "at-old"  # do registro já renovado


async def test_refresh_lock_held_until_timeout_returns_none(fake_redis, monkeypatch):
    """P2: lock ocupado e registro nunca fica válido → após esgotar o poll,
    retorna None (não trava indefinidamente)."""
    monkeypatch.setattr(govbr_token, "_LOCK_WAIT", 0.0)
    monkeypatch.setattr(govbr_token, "_LOCK_MAX_POLLS", 3)
    fake_redis.store["govbr_refresh_lock:5521999999999"] = "1"  # nunca libera
    fake_redis.store["govbr_token:5521999999999"] = json.dumps(_record(-10, 3600))

    async def fake_post(_rt):
        raise AssertionError("não deveria postar sem o lock")

    monkeypatch.setattr(govbr_token, "_post_refresh", fake_post)
    out = await govbr_token.refresh_token("+5521999999999")
    assert out is None


async def test_refresh_logout_during_post_does_not_resurrect(fake_redis, monkeypatch):
    """P2: se revoke_token() apaga o registro durante o POST, o refresh NÃO
    ressuscita a sessão deslogada."""
    key = "govbr_token:5521999999999"
    fake_redis.store[key] = json.dumps(_record(-10, 3600, refresh_token="rt-old"))

    async def fake_post(_rt):
        fake_redis.store.pop(key, None)  # logout concorrente
        return {"access_token": "at-new", "expires_in": 300, "refresh_expires_in": 7200}

    monkeypatch.setattr(govbr_token, "_post_refresh", fake_post)
    out = await govbr_token.refresh_token("+5521999999999")
    assert out is None
    assert key not in fake_redis.store  # não ressuscitou


async def test_refresh_reauth_during_post_does_not_clobber(fake_redis, monkeypatch):
    """P2: se um novo callback grava token fresco (refresh_token diferente)
    durante o POST, o refresh NÃO sobrescreve o login novo."""
    key = "govbr_token:5521999999999"
    fake_redis.store[key] = json.dumps(_record(-10, 3600, refresh_token="rt-old"))

    async def fake_post(_rt):
        fresh = _record(300, 7200, refresh_token="rt-brandnew")
        fresh["access_token"] = "at-fresh"
        fake_redis.store[key] = json.dumps(fresh)  # re-auth concorrente
        return {
            "access_token": "at-stale",
            "expires_in": 300,
            "refresh_expires_in": 7200,
        }

    monkeypatch.setattr(govbr_token, "_post_refresh", fake_post)
    out = await govbr_token.refresh_token("+5521999999999")
    assert out is not None
    assert out["access_token"] == "at-fresh"  # devolve o login novo
    assert not fake_redis.setex_calls  # não regravou por cima
    stored = json.loads(fake_redis.store[key])
    assert stored["access_token"] == "at-fresh"


async def test_release_lock_respects_ownership(fake_redis):
    """P2: só libera o lock se ainda formos o dono — não apaga o de outro worker."""
    fake_redis.store["lk"] = "other-worker-token"
    await govbr_token._release_lock(fake_redis, "lk", "my-token")
    assert fake_redis.store.get("lk") == "other-worker-token"  # NÃO apagou

    fake_redis.store["lk2"] = "my-token"
    await govbr_token._release_lock(fake_redis, "lk2", "my-token")
    assert "lk2" not in fake_redis.store  # apagou o próprio


async def test_save_token_uses_long_ttl(fake_redis):
    await govbr_token.save_token(
        user_number="+5521999999999",
        access_token="at",
        refresh_token="rt",
        expires_in=300,
        refresh_expires_in=7200,
        service_context="iptu",
    )
    assert fake_redis.setex_calls, "esperava setex"
    _key, ttl, _value = fake_redis.setex_calls[-1]
    assert ttl == 7200  # max(access=300, refresh=7200) — refresh sobrevive ao access
