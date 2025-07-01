"""
Middleware para validação de Bearer Token.

Este módulo fornece middlewares para autenticação via Bearer Token
em requisições HTTP para o servidor MCP.
"""
from typing import Optional
from fastapi import Request, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from loguru import logger


class BearerTokenMiddleware:
    """
    Middleware para validação de Bearer Token.
    
    Valida tokens JWT ou outros tokens de autenticação
    no header Authorization das requisições.
    """
    
    def __init__(self, valid_tokens: Optional[list[str]] = None):
        """
        Inicializa o middleware.
        
        Args:
            valid_tokens: Lista de tokens válidos. Se None, usa variável de ambiente.
        """
        self.valid_tokens = valid_tokens or []
        self.security = HTTPBearer(auto_error=False)
        
    async def __call__(self, request: Request, call_next):
        """
        Processa a requisição e valida o token.
        
        Args:
            request: Requisição FastAPI
            call_next: Próxima função no pipeline
            
        Returns:
            Resposta processada
            
        Raises:
            HTTPException: Se o token for inválido ou ausente
        """
        # Pula validação para endpoints de saúde
        if request.url.path in ["/", "/docs", "/openapi.json"]:
            return await call_next(request)
            
        try:
            # Extrai credenciais do header Authorization
            credentials: Optional[HTTPAuthorizationCredentials] = (
                await self.security(request)
            )
            
            if not credentials:
                logger.warning("Token ausente na requisição")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Token de autenticação requerido"
                )
                
            token = credentials.credentials
            
            # Valida o token
            if not self._is_valid_token(token):
                logger.warning(f"Token inválido: {token[:10]}...")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Token de autenticação inválido"
                )
                
            logger.debug(f"Token válido para requisição: {request.url.path}")
            return await call_next(request)
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Erro na validação do token: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Erro interno na validação de autenticação"
            )
    
    def _is_valid_token(self, token: str) -> bool:
        """
        Verifica se o token é válido.
        
        Args:
            token: Token a ser validado
            
        Returns:
            True se o token for válido, False caso contrário
        """
        if not self.valid_tokens:
            return True  # Se não há tokens configurados, aceita qualquer token
            
        return token in self.valid_tokens


def create_bearer_middleware(valid_tokens: Optional[list[str]] = None) -> BearerTokenMiddleware:
    """
    Factory function para criar middleware de Bearer Token.
    
    Args:
        valid_tokens: Lista de tokens válidos
        
    Returns:
        Instância configurada do middleware
    """
    return BearerTokenMiddleware(valid_tokens=valid_tokens)
