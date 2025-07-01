from fastapi import HTTPException
from fastmcp.server.middleware import Middleware, MiddlewareContext

from src.config import env

class CheckTokenMiddleware(Middleware):
    async def on_request(self, context: MiddlewareContext, call_next):
        # Obtém o header Authorization
        auth_header = context.fastmcp_context.get_http_request().headers.get("Authorization")
        
        if not auth_header:
            raise HTTPException(status_code=401, detail="Token de autorização não fornecido")
        
        # Verifica se segue o formato "Bearer <token>"
        if not auth_header.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Formato de token inválido")
        
        # Extrai o token
        token = auth_header[7:]  # Remove "Bearer "
        
        # Verifica se o token é válido
        valid_tokens = env.VALID_TOKENS
        if isinstance(valid_tokens, str):
            # Se VALID_TOKENS é uma string, pode ser um único token ou vários separados por vírgula
            valid_tokens_list = [t.strip() for t in valid_tokens.split(",")]
        else:
            # Se já é uma lista
            valid_tokens_list = valid_tokens
        
        if token not in valid_tokens_list:
            raise HTTPException(status_code=401, detail="Token inválido")
        
        return await call_next(context)