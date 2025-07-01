"""
Servidor HTTP para expor o FastMCP via ASGI.
"""
from src.app import http_app
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

# Endpoint de saúde
async def health(request):
    return JSONResponse({"status": "ok", "service": "Rio MCP Server"})

# Endpoint raiz com informações básicas
async def root(request):
    return JSONResponse({
        "service": "Rio MCP Server",
        "version": "1.0.0",
        "mcp_endpoint": "/mcp",
        "health_endpoint": "/health",
        "status": "ready"
    })

# Adicionar CORS
middleware = [
    Middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        allow_credentials=True,
    )
]

# Criar app Starlette com CORS e rotas próprias
asgi_app = Starlette(
    middleware=middleware,
    routes=[
        Route("/", root, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
    ],
    lifespan=http_app.lifespan,  # IMPORTANTE: passar o lifespan do FastMCP
)

# Montar o FastMCP app no endpoint /mcp
asgi_app.mount("/mcp", http_app)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(asgi_app, host="0.0.0.0", port=8000) 