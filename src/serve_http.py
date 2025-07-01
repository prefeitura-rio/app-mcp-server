"""
Servidor HTTP para expor o FastMCP via ASGI (FastAPI + Uvicorn).

Execute:
    uv run python -m src.serve_http --host 0.0.0.0 --port 8000

O Swagger estará disponível em /docs.
"""
from uvicorn import run

from .app import app as mcp_app
from .config import Settings

from fastapi import FastAPI
from fastmcp.server.http import create_streamable_http_app

# Cria sub-aplicação Starlette para o protocolo MCP
mcp_subapp = create_streamable_http_app(mcp_app, "/")

# FastAPI principal com docs automáticos
api = FastAPI(
    title="Rio de Janeiro MCP Server",
    version=Settings.VERSION,
    description="Servidor FastMCP exposto via HTTP+Streamable (endpoint em /mcp).",
    lifespan=mcp_subapp.lifespan,
)

@api.get("/health")
def root() -> dict[str, str]:
    """Endpoint simples de saúde"""
    return {"status": "ok", "mcp": "/mcp"}

# Monta o sub-app MCP
api.mount("/mcp", mcp_subapp)

# ASGI app exportado para Uvicorn
asgi_app = api


def main() -> None:
    """Roda o servidor HTTP localmente (bloqueante)."""
    run(
        "src.serve_http:asgi_app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        workers=1,
        factory=False,
        log_level="info",
    )


if __name__ == "__main__":
    main() 