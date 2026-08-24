"""Rotas HTTP de health, com três semânticas deliberadamente separadas.

| Rota             | Papel      | Faz I/O?          | Pode retornar 503? |
|------------------|------------|--------------------|--------------------|
| `/health`        | liveness   | não                | não                |
| `/health/ready`  | readiness  | sim (Redis, c/teto)| sim                |
| `/health/detail` | diagnóstico| sim                | não                |

`/health` continua sem tocar dependência alguma: uma checagem ali faria o
kubelet **matar** o pod por uma falha que só quebra parte das tools. Já
`/health/ready` passou a sondar o Redis (com teto de tempo — ver
`src/health/state.py::evaluate_readiness`) desde que produção deixou de
rodar com `replicas: 1` (Task 4 do plano de resiliência do MCP): tirar do
balanceador só o pod que não alcança sua única dependência sem fallback é
correto quando há réplicas saudáveis para assumir o tráfego. A sondagem não
depende de exportação de telemetria (OTel/coletor) em nenhum ponto.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from src.config.settings import Settings
from src.health.models import STATUS_OK, aggregate_status
from src.health.registry import health_registry
from src.health.state import evaluate_readiness, is_ready, uptime_seconds


async def health(request: Request) -> PlainTextResponse:
    """Liveness. Responde enquanto o processo estiver de pé e com o event
    loop atendendo — nenhuma dependência é consultada, por design."""
    return PlainTextResponse("OK")


async def ready(request: Request) -> JSONResponse:
    """Readiness: processo inicializado E Redis alcançável dentro do teto.

    O corpo nunca carrega o texto de exceção nem host/URL do Redis — só um
    `reason` de um conjunto fechado (`starting`, `redis_unavailable`), o
    bastante para diagnóstico sem repetir o que `/health/detail` já sanitiza
    com mais detalhe. Ver `src/health/state.py` para o porquê deste desenho
    ter mudado a partir da Task 7 do plano de resiliência do MCP.
    """
    ready_for_traffic, reason = await evaluate_readiness()
    if ready_for_traffic:
        return JSONResponse({"status": "ready"}, status_code=200)
    return JSONResponse({"status": "not_ready", "reason": reason}, status_code=503)


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
