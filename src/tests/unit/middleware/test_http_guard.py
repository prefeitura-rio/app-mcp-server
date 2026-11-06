"""`require_authenticated` isolado: as decisões que o app real não exercita.

O caminho de produção (rotas de Dívida Ativa com o `HybridTokenVerifier`
plugado) é testado pelo app ASGI em `src/tests/unit/app/test_divida_ativa_auth.py`.
Aqui ficam a falha fechada sem middleware de auth e o desvio de `IS_LOCAL`.
"""

import importlib

import pytest
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from starlette.applications import Starlette
from starlette.authentication import AuthCredentials, SimpleUser
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient


@pytest.fixture
def guard():
    return importlib.import_module("src.middleware.http_guard")


def _app(guard, user=None):
    """App mínimo com uma rota protegida e, opcionalmente, um usuário no escopo."""
    chamadas = []

    @guard.require_authenticated
    async def rota(request):
        chamadas.append(request.url.path)
        return JSONResponse({"ok": True})

    middleware = []
    if user is not None:

        class _ComUsuario:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                if scope["type"] == "http":
                    scope["user"] = user
                    scope["auth"] = AuthCredentials([])
                await self.app(scope, receive, send)

        middleware.append(Middleware(_ComUsuario))

    app = Starlette(
        routes=[Route("/protegida", rota, methods=["POST"])], middleware=middleware
    )
    return TestClient(app), chamadas


def test_sem_middleware_de_auth_a_falha_e_fechada(guard, monkeypatch):
    """Sem `user` no escopo (provider de auth desmontado), nunca libera."""
    monkeypatch.setattr(guard.env, "IS_LOCAL", False)
    client, chamadas = _app(guard)

    response = client.post(
        "/protegida", json={}, headers={"Authorization": "Bearer qualquer"}
    )

    assert response.status_code == 401
    assert chamadas == []


def test_usuario_nao_autenticado_do_starlette_nao_passa(guard, monkeypatch):
    """Um `SimpleUser` qualquer não é o `AuthenticatedUser` do verifier."""
    monkeypatch.setattr(guard.env, "IS_LOCAL", False)
    client, chamadas = _app(guard, user=SimpleUser("intruso"))

    response = client.post("/protegida", json={})

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert chamadas == []


def test_usuario_autenticado_pelo_verifier_passa(guard, monkeypatch):
    monkeypatch.setattr(guard.env, "IS_LOCAL", False)
    user = AuthenticatedUser(
        AccessToken(token="t", client_id="legacy-static-token", scopes=[])
    )
    client, chamadas = _app(guard, user=user)

    response = client.post("/protegida", json={})

    assert response.status_code == 200
    assert chamadas == ["/protegida"]


def test_execucao_local_segue_sem_auth_como_o_mcp(guard, monkeypatch):
    """Em `IS_LOCAL` o FastMCP sobe sem provider de auth; a rota acompanha."""
    monkeypatch.setattr(guard.env, "IS_LOCAL", True)
    client, chamadas = _app(guard)

    response = client.post("/protegida", json={})

    assert response.status_code == 200
    assert chamadas == ["/protegida"]


def test_401_nao_ecoa_o_token_recebido(guard, monkeypatch):
    monkeypatch.setattr(guard.env, "IS_LOCAL", False)
    client, _ = _app(guard)

    response = client.post(
        "/protegida", json={}, headers={"Authorization": "Bearer segredo-123"}
    )

    assert response.status_code == 401
    assert "segredo-123" not in response.text
    assert "segredo-123" not in response.headers["WWW-Authenticate"]


# --- `RequireAuthenticatedByDefault`: nega por padrão (CHATR-311) ----------


def _app_com_guard(guard, user=None):
    """App com rotas que NÃO usam o decorator, atrás do middleware que nega
    por padrão — o caso de uma `custom_route` nova que esqueceu a proteção."""
    chamadas = []

    async def rota(request):
        chamadas.append(request.url.path)
        return JSONResponse({"ok": True})

    paths = ["/health", "/health/ready", "/health/detail", "/rota_nova"]
    paths += ["/mcp", "/mcp/sub", "/mcp-debug"]
    routes = [Route(p, rota, methods=["GET", "POST"]) for p in paths]

    middleware = []
    if user is not None:

        class _ComUsuario:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                if scope["type"] == "http":
                    scope["user"] = user
                await self.app(scope, receive, send)

        middleware.append(Middleware(_ComUsuario))
    middleware.append(Middleware(guard.RequireAuthenticatedByDefault))

    return TestClient(Starlette(routes=routes, middleware=middleware)), chamadas


def _usuario_do_verifier():
    return AuthenticatedUser(
        AccessToken(token="t", client_id="legacy-static-token", scopes=[])
    )


@pytest.mark.parametrize("path", ["/health", "/health/ready"])
def test_guard_libera_liveness_e_readiness_sem_token(guard, path):
    client, chamadas = _app_com_guard(guard)

    assert client.get(path).status_code == 200
    assert chamadas == [path]


@pytest.mark.parametrize("path", ["/mcp", "/mcp/sub"])
def test_guard_delega_o_mcp_ao_fastmcp(guard, path):
    client, chamadas = _app_com_guard(guard)

    assert client.post(path).status_code == 200
    assert chamadas == [path]


@pytest.mark.parametrize("path", ["/health/detail", "/rota_nova", "/mcp-debug"])
def test_guard_nega_por_padrao_sem_token(guard, path):
    """`/health/detail` deixa de ser público (R12); rota sem decorator não nasce
    pública; `/mcp-debug` não herda a isenção do `/mcp`."""
    client, chamadas = _app_com_guard(guard)

    response = client.post(path)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["error"] == "invalid_token"
    assert chamadas == []


def test_guard_token_recusado_marca_invalid_token(guard):
    client, chamadas = _app_com_guard(guard)

    response = client.post(
        "/rota_nova", headers={"Authorization": "Bearer segredo-123"}
    )

    assert response.status_code == 401
    assert 'error="invalid_token"' in response.headers["WWW-Authenticate"]
    assert "segredo-123" not in response.text
    assert chamadas == []


def test_guard_nao_aceita_usuario_que_nao_e_do_verifier(guard):
    client, chamadas = _app_com_guard(guard, user=SimpleUser("intruso"))

    assert client.post("/rota_nova").status_code == 401
    assert chamadas == []


@pytest.mark.parametrize("path", ["/health/detail", "/rota_nova"])
def test_guard_libera_usuario_autenticado(guard, path):
    client, chamadas = _app_com_guard(guard, user=_usuario_do_verifier())

    assert client.get(path).status_code == 200
    assert chamadas == [path]
