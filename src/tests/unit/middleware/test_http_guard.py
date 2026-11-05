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
