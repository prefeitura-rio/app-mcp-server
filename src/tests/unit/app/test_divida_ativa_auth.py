"""Autenticação das rotas HTTP de Dívida Ativa pelo app ASGI real (DEV-213).

Achado `vuln-0002` do pentest (INFRAVPIA-320): as `custom_route`s de Dívida
Ativa respondiam sem token, porque o `auth=` do FastMCP só protege o `/mcp`.
Estes testes montam o app de produção (`create_app()` com `IS_LOCAL=false`) e
fazem requisições de verdade pelo `http_app()`, então provam o que importa: a
rota servida exige o mesmo `Bearer` do `/mcp` e recusa antes de chamar a PGM.

O Keycloak é configurado com um JWKS local (nenhuma rede), para exercitar os
dois caminhos do `HybridTokenVerifier`: token estático e JWT com `azp`.
"""

import importlib
import sys

import httpx
import pytest
from authlib.jose import JsonWebKey
from fastmcp.server.auth.providers.jwt import RSAKeyPair
from starlette.testclient import TestClient

from src.health import state as health_state


STATIC_TOKEN = "token-estatico-de-teste"
FAKE_ISSUER = "https://keycloak.example.org/realms/carioca"
FAKE_JWKS_URI = f"{FAKE_ISSUER}/protocol/openid-connect/certs"
TRUSTED_AZP = "salesforce-mcp-client"

# Rota -> nome, em `src.app`, da função de negócio que ela chama. As rotas
# resolvem esse nome no módulo a cada requisição, então trocá-lo no módulo
# basta para observar se a PGM seria chamada.
DIVIDA_ATIVA_ROUTES = {
    "/consulta_debitos": "consultar_debitos",
    "/emitir_guia": "emitir_guia_a_vista",
    "/emitir_guia_regularizacao": "emitir_guia_regularizacao",
    "/v2/emitir_guia": "emitir_guia_a_vista_v2",
    "/v2/emitir_guia_regularizacao": "emitir_guia_regularizacao_v2",
}

# Dado sintético: nenhum CPF, inscrição ou CDA real.
PAYLOAD = {"consulta_debitos": "inscricaoImobiliaria", "inscricaoImobiliaria": "0"}


@pytest.fixture(scope="module")
def key_pair():
    return RSAKeyPair.generate()


@pytest.fixture
def app_env(monkeypatch, key_pair):
    """Importa `src.app` em produção (sem `IS_LOCAL`) com Keycloak configurado.

    Mesma higiene do `test_app_factory.py`: `src.config.env` lê o ambiente no
    import, então `src.*` é purgado antes e o snapshot volta no fim.
    """
    for nome, valor in {
        "IS_LOCAL": "false",
        "VALID_TOKENS": STATIC_TOKEN,
        "KEYCLOAK_ISSUER": FAKE_ISSUER,
        "KEYCLOAK_JWKS_URI": FAKE_JWKS_URI,
        "KEYCLOAK_TRUSTED_CLIENTS": TRUSTED_AZP,
    }.items():
        monkeypatch.setenv(nome, valor)

    jwks = {
        "keys": [JsonWebKey.import_key(key_pair.public_key, {"kty": "RSA"}).as_dict()]
    }

    class _JWKSResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return jwks

    async def fake_get(self, url, *args, **kwargs):
        assert url == FAKE_JWKS_URI
        return _JWKSResponse()

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    ready_antes = health_state.is_ready()
    snapshot = {
        name: mod for name, mod in sys.modules.items() if name.startswith("src")
    }
    for name in list(sys.modules):
        if name.startswith("src"):
            del sys.modules[name]
    try:
        yield importlib.import_module("src.app")
    finally:
        for name in list(sys.modules):
            if name.startswith("src"):
                del sys.modules[name]
        sys.modules.update(snapshot)
        health_state.set_ready(ready_antes)


@pytest.fixture
def chamadas(monkeypatch, app_env):
    """Troca as funções de negócio por duplos que registram a chamada."""
    registro = []

    for nome in set(DIVIDA_ATIVA_ROUTES.values()):

        def _duplo(parameters, _nome=nome):
            registro.append(_nome)

            async def _resposta():
                return {"api_resposta_sucesso": True, "duplo": _nome}

            return _resposta()

        monkeypatch.setattr(app_env, nome, _duplo)
    return registro


@pytest.fixture
def client(app_env, chamadas):
    # Sem `with`: o lifespan (Redis, BigQuery, line-up) não sobe. As rotas de
    # Dívida Ativa e o middleware de auth não dependem dele.
    return TestClient(app_env.create_app().http_app())


def _jwt(key_pair, **claims) -> str:
    return key_pair.create_token(issuer=FAKE_ISSUER, additional_claims=claims)


@pytest.mark.parametrize("route", list(DIVIDA_ATIVA_ROUTES))
def test_sem_authorization_responde_401_sem_chamar_a_pgm(client, chamadas, route):
    response = client.post(route, json=PAYLOAD)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["error"] == "invalid_token"
    assert chamadas == []


@pytest.mark.parametrize("route", list(DIVIDA_ATIVA_ROUTES))
@pytest.mark.parametrize(
    "authorization",
    [
        "Bearer token-que-nao-existe",
        "Bearer ",
        f"Basic {STATIC_TOKEN}",
        STATIC_TOKEN,
        f"Bearer {STATIC_TOKEN}x",
    ],
    ids=["desconhecido", "vazio", "esquema-basic", "sem-esquema", "prefixo"],
)
def test_token_invalido_responde_401_sem_chamar_a_pgm(
    client, chamadas, route, authorization
):
    response = client.post(
        route, json=PAYLOAD, headers={"Authorization": authorization}
    )

    assert response.status_code == 401
    assert 'error="invalid_token"' in response.headers["WWW-Authenticate"]
    assert chamadas == []


@pytest.mark.parametrize("route", list(DIVIDA_ATIVA_ROUTES))
def test_jwt_expirado_responde_401(client, chamadas, key_pair, route):
    token = key_pair.create_token(
        issuer=FAKE_ISSUER,
        additional_claims={"azp": TRUSTED_AZP},
        expires_in_seconds=-60,
    )

    response = client.post(
        route, json=PAYLOAD, headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 401
    assert chamadas == []


def test_jwt_com_azp_fora_da_allowlist_responde_401(client, chamadas, key_pair):
    token = _jwt(key_pair, azp="client-nao-autorizado")

    response = client.post(
        "/v2/emitir_guia", json=PAYLOAD, headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 401
    assert chamadas == []


def test_jwt_assinado_por_outra_chave_responde_401(client, chamadas):
    token = RSAKeyPair.generate().create_token(
        issuer=FAKE_ISSUER, additional_claims={"azp": TRUSTED_AZP}
    )

    response = client.post(
        "/consulta_debitos",
        json=PAYLOAD,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    assert chamadas == []


def test_401_sai_antes_de_ler_o_corpo(client, chamadas):
    """Corpo inválido sem token é 401, não o 500 do `request.json()`."""
    response = client.post(
        "/consulta_debitos",
        content=b"{nao-e-json",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 401
    assert chamadas == []


@pytest.mark.parametrize(("route", "funcao"), list(DIVIDA_ATIVA_ROUTES.items()))
def test_token_estatico_valido_chega_a_rota(client, chamadas, route, funcao):
    response = client.post(
        route, json=PAYLOAD, headers={"Authorization": f"Bearer {STATIC_TOKEN}"}
    )

    assert response.status_code == 200
    assert response.json() == {"api_resposta_sucesso": True, "duplo": funcao}
    assert chamadas == [funcao]


@pytest.mark.parametrize(("route", "funcao"), list(DIVIDA_ATIVA_ROUTES.items()))
def test_jwt_do_keycloak_com_azp_permitido_chega_a_rota(
    client, chamadas, key_pair, route, funcao
):
    token = _jwt(key_pair, azp=TRUSTED_AZP)

    response = client.post(
        route, json=PAYLOAD, headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert chamadas == [funcao]


def test_mcp_continua_exigindo_token(client):
    """Sem regressão no `/mcp`: continua 401 sem token e com token inválido."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1"},
        },
    }
    headers = {"Accept": "application/json, text/event-stream"}

    sem_token = client.post("/mcp", json=body, headers=headers)
    invalido = client.post(
        "/mcp",
        json=body,
        headers={**headers, "Authorization": "Bearer token-que-nao-existe"},
    )

    assert sem_token.status_code == 401
    assert invalido.status_code == 401
    assert "invalid_token" in invalido.headers["WWW-Authenticate"]


def test_health_continua_publico(client):
    """Liveness é do kubelet, que não manda token."""
    response = client.get("/health")

    assert response.status_code == 200
    assert response.text == "OK"
