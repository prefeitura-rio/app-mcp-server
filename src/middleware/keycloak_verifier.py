"""Verificador de JWT do Keycloak ("Identidade Carioca") restrito por azp.

Estende o `JWTVerifier` nativo do FastMCP — que já valida assinatura via
JWKS, `iss` e `exp` — para também restringir a autenticação por `azp` (o
client_id que emitiu o token), replicando o padrão
`TrustedServiceClients`/`azp` já usado no app-rmi (Go).

Diferença deliberada em relação ao app-rmi: lá a verificação de assinatura é
opcional porque o serviço confia em uma camada Cerbos/Istio a montante. Este
servidor não está atrás dessa camada, então aqui a verificação de assinatura
via JWKS é obrigatória e feita de verdade pelo `JWTVerifier` nativo.
"""

from __future__ import annotations

from fastmcp.server.auth import AccessToken, JWTVerifier


class AzpConstrainedJWTVerifier(JWTVerifier):
    """`JWTVerifier` que também restringe o token por client_id (`azp`).

    Se `allowed_azp` estiver vazia, qualquer client autenticado com sucesso
    contra o realm é aceito (comportamento permissivo explícito, sinalizado
    via log de warning no startup do servidor — ver `src/app.py`).
    """

    def __init__(self, *, allowed_azp: list[str] | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.allowed_azp: list[str] = allowed_azp or []

    async def load_access_token(self, token: str) -> AccessToken | None:
        access_token = await super().load_access_token(token)
        if access_token is None or not self.allowed_azp:
            # Token inválido (já rejeitado pelo JWTVerifier nativo) ou sem
            # allowlist configurada: aceita qualquer client válido do realm.
            return access_token
        if access_token.claims.get("azp") not in self.allowed_azp:
            return None
        return access_token
