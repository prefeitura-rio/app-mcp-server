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


# Rotas que o middleware abaixo deixa passar sem usuário autenticado. Liveness e
# readiness são consultadas pelo kubelet sem credencial e só dizem "o processo
# responde". `/health/detail` fica de fora de propósito: descreve as
# dependências do serviço e passa a exigir token (CHATR-311, R12).
PUBLIC_PATHS: frozenset[str] = frozenset({"/health", "/health/ready"})

# `/mcp` já tem o `RequireAuthMiddleware` do próprio FastMCP, que devolve o 401
# no formato da spec do MCP; recusar aqui trocaria essa resposta pela nossa.
DELEGATED_PREFIX = "/mcp"


def _isenta(path: str) -> bool:
    """Allowlist do middleware. `/mcp` casa por segmento: `/mcp-debug` não é
    `/mcp` e não pode herdar a isenção."""
    if path in PUBLIC_PATHS:
        return True
    return path == DELEGATED_PREFIX or path.startswith(DELEGATED_PREFIX + "/")


class RequireAuthenticatedByDefault:
    """Middleware ASGI que nega por padrão: fora da allowlist, exige usuário
    autenticado em qualquer rota.

    `require_authenticated` protege as rotas em que foi aplicado; uma
    `custom_route` nova sem o decorator nasceria pública, como nasceram as de
    Dívida Ativa (`vuln-0002`). Este middleware fecha esse caminho: a rota nova
    já sai protegida, e o decorator segue nas rotas existentes como segunda
    camada.

    Usa o mesmo critério e a mesma resposta 401 do decorator (`scope["user"]`
    populado pelo `AuthenticationMiddleware` do FastMCP, que fica por fora
    deste na pilha). ASGI puro, e não `BaseHTTPMiddleware`, para não bufferizar
    o streaming do `/mcp`. Montado em `src/main.py` só fora do modo local.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if (
            scope["type"] != "http"
            or _isenta(scope.get("path", ""))
            or isinstance(scope.get("user"), AuthenticatedUser)
        ):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        logger.warning(
            f"Requisição sem autenticação válida recusada em {request.url.path}"
        )
        await _unauthorized(request)(scope, receive, send)
