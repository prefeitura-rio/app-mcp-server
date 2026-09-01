"""Verificador híbrido: JWT do Keycloak ("Identidade Carioca") OU token
estático legado.

Tenta validar o token como JWT do Keycloak primeiro; qualquer falha (não é
JWT, assinatura inválida, issuer errado, `azp` fora da allowlist, ou
Keycloak ainda não configurado) cai para o comportamento atual de
comparação contra `VALID_TOKENS`, preservando 100% de compatibilidade com
os consumidores existentes.
"""

from __future__ import annotations

import hmac

from fastmcp.server.auth import AccessToken, TokenVerifier

from src.middleware.keycloak_verifier import AzpConstrainedJWTVerifier


def _mesmo_token(apresentado: str, valido: str) -> bool:
    """Compara dois tokens em tempo constante.

    `token in self._static_tokens` decide por hash e, quando os hashes batem,
    cai num `str.__eq__` que compara tamanho antes do conteúdo e sai no
    primeiro byte divergente. O sinal que isso deixa é estreito — o ruído de
    rede o cobre com folga, e o servidor não está exposto à internet aberta —
    mas o `VALID_TOKENS` é segredo compartilhado, sem escopo, sem expiração e
    sem rotação: um vazamento aqui não tem contenção depois. `compare_digest`
    custa uma varredura sobre um punhado de tokens e tira o argumento da mesa.

    Compara em bytes de propósito: `compare_digest` recusa `str` com caractere
    fora do ASCII, e o token chega de um header decodificado em latin-1 — um
    byte alto no `Authorization` viraria `TypeError`, isto é, 500 no lugar de
    401. `surrogatepass` fecha o caso restante (surrogate solto) sem mapear
    dois tokens distintos para os mesmos bytes.
    """
    return hmac.compare_digest(
        apresentado.encode("utf-8", "surrogatepass"),
        valido.encode("utf-8", "surrogatepass"),
    )


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
        # Varre a lista inteira quando não há match — é o caminho que um
        # atacante mede. O `any` só encurta quando o token já é válido, e aí
        # não há o que descobrir.
        if not any(_mesmo_token(token, valido) for valido in self._static_tokens):
            return None
        return AccessToken(
            token=token,
            client_id="legacy-static-token",
            scopes=[],
            claims={"auth_method": "static"},
        )
