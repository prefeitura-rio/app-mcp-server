"""
Servidor HTTP para expor o FastMCP via ASGI.
"""
from .app import http_app
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.applications import Starlette

# Adicionar CORS ao http_app
middleware = [
    Middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )
]

# Criar app Starlette com CORS e montar o FastMCP
asgi_app = Starlette(
    middleware=middleware,
    routes=[],
)

# Montar o FastMCP app diretamente na raiz
asgi_app.mount("/", http_app)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(asgi_app, host="0.0.0.0", port=8000) 