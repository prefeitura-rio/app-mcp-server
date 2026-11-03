"""Autenticação das rotas HTTP próprias (`@mcp.custom_route`).

O `auth=` do FastMCP protege só o endpoint `/mcp`: as `custom_route`s são
servidas sem nenhuma checagem. O provider de auth, porém, instala o
`AuthenticationMiddleware` do Starlette no app inteiro, com o mesmo
`HybridTokenVerifier` do `/mcp` (JWT do Keycloak com `azp` permitido ou token
de `VALID_TOKENS`). Ele já decide, em toda requisição, se o `Bearer` é válido
e deixa o resultado em `request.scope["user"]`: aqui só se exige que essa
decisão tenha sido positiva. Nenhuma lógica de token é duplicada.
"""

from __future__ import annotations

from functools import wraps
from typing import Awaitable, Callable

from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from src.config import env
from src.utils.log import logger

Handler = Callable[[Request], Awaitable[Response]]


def _unauthorized(request: Request) -> JSONResponse:
    """401 no formato da RFC 6750 §3.1, como o `/mcp` responde.

    Sem `Authorization`, o desafio vai sem `error`; com um token recusado
    (inválido, expirado, `azp` fora da allowlist, esquema que não é `Bearer`),
    vai com `error="invalid_token"`.
    """
    if request.headers.get("authorization"):
        description = "Token de autorização inválido ou expirado"
        # Cabeçalho HTTP só em ASCII: a descrição em português vai no corpo.
        challenge = (
            'Bearer error="invalid_token", error_description="Invalid or expired token"'
        )
    else:
        description = "Token de autorização não fornecido"
        challenge = "Bearer"
    return JSONResponse(
        {"error": "invalid_token", "error_description": description},
        status_code=401,
        headers={"WWW-Authenticate": challenge},
    )


def require_authenticated(handler: Handler) -> Handler:
    """Exige `Authorization: Bearer` válido numa `custom_route`.

    Aplique ABAIXO de `@mcp.custom_route(...)`: o decorator de rota registra o
    que recebe, então precisa receber a função já protegida.

    A resposta 401 sai antes de o corpo ser lido, então nada do pedido chega
    ao handler nem à PGM. Fora do ambiente local, a ausência de um usuário
    autenticado no escopo é sempre 401, inclusive se o middleware de auth não
    estiver montado: a falha é fechada.
    """

    @wraps(handler)
    async def wrapper(request: Request) -> Response:
        # Mesmo critério do `/mcp`: em execução local o FastMCP sobe sem
        # provider de auth (ver `create_app`).
        if env.IS_LOCAL:
            return await handler(request)
        if isinstance(request.scope.get("user"), AuthenticatedUser):
            return await handler(request)
        logger.warning(
            f"Requisição sem autenticação válida recusada em {request.url.path}"
        )
        return _unauthorized(request)

    return wrapper
