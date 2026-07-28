"""
Ponto de entrada principal para o servidor FastMCP do Rio de Janeiro.
"""

import sys
from pathlib import Path

# Adiciona o diretório raiz do projeto ao Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.app import mcp
from src.config import env
from src.observability.tracing import is_tracing_enabled

if __name__ == "__main__":
    if env.IS_LOCAL:
        mcp.run()
    else:
        # `create_app()` (importado acima via `src.app`) já chamou
        # `setup_tracing()`; aqui apenas verificamos se ficou habilitado
        # para decidir se instrumenta a camada ASGI/HTTP.
        http_middleware = None
        if is_tracing_enabled():
            from starlette.middleware import Middleware as StarletteMiddleware
            from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware

            http_middleware = [StarletteMiddleware(OpenTelemetryMiddleware)]

        mcp.run(
            transport="streamable-http",
            host="0.0.0.0",
            port=80,
            path="/mcp",
            middleware=http_middleware,
            stateless_http=env.MCP_STATELESS_HTTP,
            json_response=env.MCP_JSON_RESPONSE,
        )
