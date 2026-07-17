import importlib.util
import sys
import types
from pathlib import Path

import httpx
import pytest
from authlib.jose import JsonWebKey
from fastmcp.server.auth.providers.jwt import RSAKeyPair


PROJECT_ROOT = Path(__file__).resolve().parents[4]
KEYCLOAK_VERIFIER_PATH = PROJECT_ROOT / "src" / "middleware" / "keycloak_verifier.py"
HYBRID_VERIFIER_PATH = PROJECT_ROOT / "src" / "middleware" / "hybrid_verifier.py"

FAKE_JWKS_URI = (
    "https://keycloak.example.org/realms/carioca/protocol/openid-connect/certs"
)
FAKE_ISSUER = "https://keycloak.example.org/realms/carioca"


def _load_fresh_module(monkeypatch, dotted_name, path):
    spec = importlib.util.spec_from_file_location(dotted_name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, dotted_name, module)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_hybrid_verifier_module(monkeypatch):
    """Carrega `hybrid_verifier` (e sua dependência `keycloak_verifier`)
    diretamente dos arquivos, registrando módulos `src`/`src.middleware`
    "vazios" em `sys.modules` para que os imports relativos do módulo
    (`from src.middleware...`) resolvam sem reimportar as versões já
    carregadas por `src/__init__.py` (que, na coleta normal do pytest via
    `conftest.py`, já importou `src.app` e todas as tools).

    Isso NÃO isola o teste de exigir as env vars de produção — a coleção
    do pytest já disparou essa importação completa antes deste fixture
    rodar. O único isolamento real aqui é de rede: nenhum Keycloak/JWKS
    de verdade é chamado (tudo é mockado via `httpx` + `RSAKeyPair`
    gerado localmente).
    """
    src_pkg = types.ModuleType("src")
    src_pkg.__path__ = [str(PROJECT_ROOT / "src")]
    middleware_pkg = types.ModuleType("src.middleware")
    middleware_pkg.__path__ = [str(PROJECT_ROOT / "src" / "middleware")]

    monkeypatch.setitem(sys.modules, "src", src_pkg)
    monkeypatch.setitem(sys.modules, "src.middleware", middleware_pkg)

    _load_fresh_module(
        monkeypatch, "src.middleware.keycloak_verifier", KEYCLOAK_VERIFIER_PATH
    )
    return _load_fresh_module(
        monkeypatch, "src.middleware.hybrid_verifier", HYBRID_VERIFIER_PATH
    )


class _FakeJWKSResponse:
    """Resposta HTTP falsa para simular o endpoint JWKS do Keycloak."""

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def mock_jwks_endpoint(monkeypatch, *public_pems):
    """Faz `httpx.AsyncClient.get` retornar um JWKS local com as chaves
    públicas informadas, sem nenhuma chamada de rede real."""
    keys = [JsonWebKey.import_key(pem, {"kty": "RSA"}).as_dict() for pem in public_pems]
    payload = {"keys": keys}

    async def fake_get(self, url, *args, **kwargs):
        return _FakeJWKSResponse(payload)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


# (a)/(e) Token estático continua autenticando exatamente como hoje, quando
# Keycloak não está configurado — e nenhum JWTVerifier chega a ser criado.
@pytest.mark.asyncio
async def test_static_token_authenticates_without_keycloak_configured(monkeypatch):
    module = load_hybrid_verifier_module(monkeypatch)
    verifier = module.HybridTokenVerifier(
        static_tokens=["abc123", "def456"],
        jwks_uri=None,
        issuer=None,
        allowed_azp=[],
    )

    assert verifier._jwt_verifier is None

    result = await verifier.verify_token("abc123")

    assert result is not None
    assert result.client_id == "legacy-static-token"
    assert result.claims["auth_method"] == "static"

    assert await verifier.verify_token("token-desconhecido") is None


# (b) JWT válido, assinado pela chave do JWKS, com `azp` na allowlist.
@pytest.mark.asyncio
async def test_valid_jwt_with_allowed_azp_authenticates(monkeypatch):
    module = load_hybrid_verifier_module(monkeypatch)
    key_pair = RSAKeyPair.generate()
    mock_jwks_endpoint(monkeypatch, key_pair.public_key)

    verifier = module.HybridTokenVerifier(
        static_tokens=["legacy-token"],
        jwks_uri=FAKE_JWKS_URI,
        issuer=FAKE_ISSUER,
        allowed_azp=["salesforce-mcp-client"],
    )
    token = key_pair.create_token(
        issuer=FAKE_ISSUER,
        additional_claims={"azp": "salesforce-mcp-client"},
    )

    result = await verifier.verify_token(token)

    assert result is not None
    assert result.claims["azp"] == "salesforce-mcp-client"
    assert result.claims["auth_method"] == "oauth"


# (c) JWT válido e bem assinado, mas com `azp` fora da allowlist.
@pytest.mark.asyncio
async def test_jwt_with_disallowed_azp_is_rejected(monkeypatch):
    module = load_hybrid_verifier_module(monkeypatch)
    key_pair = RSAKeyPair.generate()
    mock_jwks_endpoint(monkeypatch, key_pair.public_key)

    verifier = module.HybridTokenVerifier(
        static_tokens=["legacy-token"],
        jwks_uri=FAKE_JWKS_URI,
        issuer=FAKE_ISSUER,
        allowed_azp=["salesforce-mcp-client"],
    )
    token = key_pair.create_token(
        issuer=FAKE_ISSUER,
        additional_claims={"azp": "client-nao-autorizado"},
    )

    assert await verifier.verify_token(token) is None


# (d) JWT com assinatura inválida (assinado por uma chave diferente da
# publicada no JWKS do Keycloak) é rejeitado.
@pytest.mark.asyncio
async def test_jwt_with_invalid_signature_is_rejected(monkeypatch):
    module = load_hybrid_verifier_module(monkeypatch)
    trusted_key_pair = RSAKeyPair.generate()
    attacker_key_pair = RSAKeyPair.generate()
    mock_jwks_endpoint(monkeypatch, trusted_key_pair.public_key)

    verifier = module.HybridTokenVerifier(
        static_tokens=["legacy-token"],
        jwks_uri=FAKE_JWKS_URI,
        issuer=FAKE_ISSUER,
        allowed_azp=["salesforce-mcp-client"],
    )
    forged_token = attacker_key_pair.create_token(
        issuer=FAKE_ISSUER,
        additional_claims={"azp": "salesforce-mcp-client"},
    )

    assert await verifier.verify_token(forged_token) is None


# (e) Sem KEYCLOAK_JWKS_URI/KEYCLOAK_ISSUER configurados, o comportamento é
# idêntico ao atual (só token estático), sem tentar nenhuma chamada de rede.
@pytest.mark.asyncio
async def test_no_keycloak_config_never_touches_network(monkeypatch):
    async def fail_if_called(self, *args, **kwargs):
        raise AssertionError(
            "Nenhuma chamada de rede deveria ocorrer sem Keycloak configurado"
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fail_if_called)

    module = load_hybrid_verifier_module(monkeypatch)
    verifier = module.HybridTokenVerifier(
        static_tokens=["legacy-token"],
        jwks_uri=None,
        issuer=None,
        allowed_azp=[],
    )

    assert verifier._jwt_verifier is None
    result = await verifier.verify_token("legacy-token")
    assert result is not None
    assert result.claims["auth_method"] == "static"
    assert await verifier.verify_token("qualquer-outra-coisa") is None


# Extra: consumidores com token estático continuam funcionando mesmo depois
# do Keycloak já estar configurado (coexistência híbrida real).
@pytest.mark.asyncio
async def test_static_token_still_works_when_keycloak_also_configured(monkeypatch):
    module = load_hybrid_verifier_module(monkeypatch)
    key_pair = RSAKeyPair.generate()
    mock_jwks_endpoint(monkeypatch, key_pair.public_key)

    verifier = module.HybridTokenVerifier(
        static_tokens=["legacy-static-token-xyz"],
        jwks_uri=FAKE_JWKS_URI,
        issuer=FAKE_ISSUER,
        allowed_azp=["salesforce-mcp-client"],
    )

    result = await verifier.verify_token("legacy-static-token-xyz")

    assert result is not None
    assert result.claims["auth_method"] == "static"


# Extra: allowlist de azp vazia é um opt-in explícito para aceitar qualquer
# client válido do realm (documentado em AzpConstrainedJWTVerifier).
@pytest.mark.asyncio
async def test_azp_constrained_verifier_accepts_any_client_when_allowlist_empty(
    monkeypatch,
):
    module = load_hybrid_verifier_module(monkeypatch)
    key_pair = RSAKeyPair.generate()

    verifier = module.AzpConstrainedJWTVerifier(
        public_key=key_pair.public_key,
        issuer=FAKE_ISSUER,
        algorithm="RS256",
        allowed_azp=[],
    )
    token = key_pair.create_token(
        issuer=FAKE_ISSUER, additional_claims={"azp": "qualquer-client"}
    )

    result = await verifier.load_access_token(token)

    assert result is not None
    assert result.claims["azp"] == "qualquer-client"
