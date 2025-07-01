"""
Servidor HTTP para expor o FastMCP via ASGI.
"""
from .app import http_app
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

# Endpoint de saúde
async def health(request):
    return JSONResponse({"status": "ok", "service": "Rio MCP Server"})

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

# Criar app Starlette com CORS e rota de saúde
asgi_app = Starlette(
    middleware=middleware,
    routes=[
        Route("/health", health),
    ],
    lifespan=http_app.lifespan,  # IMPORTANTE: passar o lifespan do FastMCP
)

# Montar o FastMCP app diretamente na raiz
asgi_app.mount("/", http_app)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(asgi_app, host="0.0.0.0", port=8000) 