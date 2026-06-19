from typing import Optional

from fastapi import HTTPException
from fastmcp.server.middleware import Middleware, MiddlewareContext

from src.config import env


def is_valid_bearer(auth_header: Optional[str]) -> bool:
    """Valida `Authorization: Bearer <token>` contra env.VALID_TOKENS.

    Reusado pelo CheckTokenMiddleware (pipeline de mensagens MCP) E pelas
    @mcp.custom_route Starlette — que o middleware NÃO cobre (ele só roda no
    pipeline de mensagens MCP via fastmcp_context, não em rotas Starlette).
    """
    if not auth_header or not auth_header.startswith("Bearer "):
        return False
    token = auth_header[7:]  # Remove "Bearer "
    valid_tokens = env.VALID_TOKENS
    if isinstance(valid_tokens, str):
        # string: um único token ou vários separados por vírgula
        valid_tokens_list = [t.strip() for t in valid_tokens.split(",")]
    else:
        # já é uma lista
        valid_tokens_list = valid_tokens
    return token in valid_tokens_list


class CheckTokenMiddleware(Middleware):
    async def on_request(self, context: MiddlewareContext, call_next):
        # Obtém o header Authorization
        auth_header = context.fastmcp_context.get_http_request().headers.get(
            "Authorization"
        )

        if not auth_header:
            raise HTTPException(
                status_code=401, detail="Token de autorização não fornecido"
            )

        # Verifica se segue o formato "Bearer <token>"
        if not auth_header.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Formato de token inválido")

        # Verifica se o token é válido (lógica compartilhada com is_valid_bearer)
        if not is_valid_bearer(auth_header):
            raise HTTPException(status_code=401, detail="Token inválido")

        return await call_next(context)
