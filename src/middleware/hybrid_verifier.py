"""Verificador híbrido: JWT do Keycloak ("Identidade Carioca") OU token
estático legado.

Tenta validar o token como JWT do Keycloak primeiro; qualquer falha (não é
JWT, assinatura inválida, issuer errado, `azp` fora da allowlist, ou
Keycloak ainda não configurado) cai para o comportamento atual de
comparação contra `VALID_TOKENS`, preservando 100% de compatibilidade com
os consumidores existentes.
"""

from __future__ import annotations

from fastmcp.server.auth import AccessToken, TokenVerifier

from src.middleware.keycloak_verifier import AzpConstrainedJWTVerifier


class HybridTokenVerifier(TokenVerifier):
    """Aceita JWT do Keycloak OU o token estático legado (`VALID_TOKENS`)."""

    def __init__(
        self,
        *,
        static_tokens: list[str],
        jwks_uri: str | None = None,
        issuer: str | None = None,
        allowed_azp: list[str] | None = None,
    ) -> None:
        super().__init__()
        self._static_tokens: set[str] = set(static_tokens)
        self._jwt_verifier: AzpConstrainedJWTVerifier | None = None
        if jwks_uri and issuer:
            self._jwt_verifier = AzpConstrainedJWTVerifier(
                jwks_uri=jwks_uri,
                issuer=issuer,
                algorithm="RS256",
                allowed_azp=allowed_azp,
            )

    async def verify_token(self, token: str) -> AccessToken | None:
        if self._jwt_verifier is not None:
            result = await self._jwt_verifier.load_access_token(token)
            if result is not None:
                # Atribuição direta (não `setdefault`): o servidor é a
                # autoridade sobre qual caminho autenticou o token. Uma claim
                # `auth_method` vinda do próprio JWT não deve conseguir se
                # sobrepor a essa decisão (relevante se essa claim vier a ser
                # usada para autorização por rota — ver AUTHENTICATION.md).
                result.claims["auth_method"] = "oauth"
                return result
        return self._verify_static(token)

    def _verify_static(self, token: str) -> AccessToken | None:
        if token not in self._static_tokens:
            return None
        return AccessToken(
            token=token,
            client_id="legacy-static-token",
            scopes=[],
            claims={"auth_method": "static"},
        )
