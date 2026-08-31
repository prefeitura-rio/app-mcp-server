"""
Ponto de entrada principal para o servidor FastMCP do Rio de Janeiro.
"""

import sys
from pathlib import Path

# Adiciona o diretório raiz do projeto ao Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Antes de qualquer outra coisa: instala a barreira de redação de PII do
# CHATR-167. `src.utils.log` configura o `logger` global do loguru, então a
# primeira linha logada no processo -- inclusive as do preflight -- já sai
# redigida.
import src.utils.log  # noqa: F401,E402

from src.health.preflight import run_startup_preflight

# Precisa rodar ANTES de `src.app`, que importa `src.config.env` e aborta na
# primeira variável faltante. O preflight reporta todas de uma vez e só então
# deixa (ou não) a aplicação subir.
run_startup_preflight()

from src.app import build_http_middleware, mcp  # noqa: E402
from src.config import env  # noqa: E402
from src.observability.tracing import is_tracing_enabled  # noqa: E402

if __name__ == "__main__":
    if env.IS_LOCAL:
        mcp.run()
    else:
        # `create_app()` (importado acima via `src.app`) já chamou
        # `setup_tracing()`; aqui apenas verificamos se ficou habilitado
        # para decidir se instrumenta a camada ASGI/HTTP.
        tracing_middleware = None
        if is_tracing_enabled():
            from starlette.middleware import Middleware as StarletteMiddleware
            from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware

            tracing_middleware = [StarletteMiddleware(OpenTelemetryMiddleware)]

        # `build_http_middleware()` monta a exigência de autenticação e o teto
        # de corpo, que valem para TODAS as rotas — não só `/mcp`. Montar essa
        # lista aqui, e não dentro de `create_app()`, seria repetir a decisão em
        # cada ponto de entrada; é exatamente assim que uma rota volta a nascer
        # pública.
        http_middleware = build_http_middleware(tracing_middleware)

        mcp.run(
            transport="streamable-http",
            host="0.0.0.0",
            port=env.SERVER_PORT,
            path="/mcp",
            middleware=http_middleware,
            stateless_http=env.MCP_STATELESS_HTTP,
        )
