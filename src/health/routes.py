"""Rotas HTTP de health, com três semânticas deliberadamente separadas.

| Rota             | Papel      | Faz I/O? | Pode retornar 503? |
|------------------|------------|----------|--------------------|
| `/health`        | liveness   | não      | não                |
| `/health/ready`  | readiness  | não      | sim                |
| `/health/detail` | diagnóstico| sim      | não                |

A separação existe porque hoje um único `/health` serve liveness e readiness
no Kubernetes. Colocar checagem de dependência ali faria o kubelet **matar** o
pod quando o Redis caísse — e, com `replicas: 1` em produção, uma degradação
parcial viraria indisponibilidade total. Por isso o liveness continua trivial
e a visibilidade vai para `/health/detail`.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from src.config.settings import Settings
from src.health.models import STATUS_OK, aggregate_status
from src.health.registry import health_registry
from src.health.state import is_ready, uptime_seconds


async def health(request: Request) -> PlainTextResponse:
    """Liveness. Responde enquanto o processo estiver de pé e com o event
    loop atendendo — nenhuma dependência é consultada, por design."""
    return PlainTextResponse("OK")


async def ready(request: Request) -> JSONResponse:
    """Readiness: a inicialização da aplicação foi concluída.

    Não reflete a saúde das dependências — ver `src/health/state.py`.

    O valor de manter esta rota separada de `/health` é desacoplar as duas
    semânticas no Kubernetes: enquanto os dois probes apontavam para o mesmo
    endpoint, qualquer verificação acrescentada ali passaria a poder matar o
    pod. Com a separação, readiness pode evoluir sem esse risco.
    """
    if is_ready():
        return JSONResponse({"status": "ready"}, status_code=200)
    return JSONResponse({"status": "not_ready"}, status_code=503)


async def detail(request: Request) -> JSONResponse:
    """Diagnóstico completo das dependências.

    Sempre 200: é ferramenta de observação, não probe. O status agregado vai
    no corpo. O payload não carrega URL, host nem valor de configuração — ver
    `registry.sanitize_error`.
    """
    from src.config import env

    results = await health_registry.run_all()
    return JSONResponse(
        {
            "status": aggregate_status(results),
            "environment": env.ENVIRONMENT,
            "version": Settings.VERSION,
            "uptime_s": uptime_seconds(),
            "ready": is_ready(),
            "checks": [result.to_dict() for result in results],
        },
        status_code=200,
    )


def register_health_routes(mcp: Any) -> None:
    """Registra as três rotas no servidor MCP.

    Registrado também em ambiente local (diferente do `/health` anterior, que
    era exclusivo de produção): os endpoints não têm custo quando ninguém os
    chama, e poder inspecionar dependências localmente é justamente o ponto.
    """
    mcp.custom_route("/health", methods=["GET"])(health)
    mcp.custom_route("/health/ready", methods=["GET"])(ready)
    mcp.custom_route("/health/detail", methods=["GET"])(detail)


# `STATUS_OK` é reexportado para consumidores da rota (testes, scripts).
__all__ = ["health", "ready", "detail", "register_health_routes", "STATUS_OK"]
