"""Checks de dependência em runtime, reportados por `/health/detail`.

Nenhum deles gateia tráfego — ver `src/health/state.py` para o porquê.
Cobrem só as dependências que a maioria das tools compartilha: as APIs
específicas de um workflow (Dívida Ativa, IPTU, SGRC, Maps, Gemini, ...) ficam
de fora de propósito, porque sondar uma dúzia de APIs a cada probe custa mais
do que informa — a falha delas já aparece no error interceptor e no OTel.

`src.config.env` é importado dentro das funções, e não no topo: mantém o
módulo importável em testes unitários sem exigir o ambiente completo, e segue
o padrão já usado em `src/utils/bigquery.py`.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from src.health import external_tables, preflight
from src.health.models import CheckStatus, HealthCheckError
from src.health.registry import HealthRegistry, health_registry
from src.observability import metrics
from src.utils.log import logger

# Backend Redis dedicado ao health check, criado uma única vez: o cliente
# mantém um pool de conexões, e recriá-lo a cada probe vazaria conexões.
_redis_backend: Optional[Any] = None


def _get_redis_backend():
    global _redis_backend
    if _redis_backend is None:
        from src.config import env
        from src.tools.multi_step_service.core.state import RedisBackend

        _redis_backend = RedisBackend(
            redis_url=env.REDIS_URL, ttl_seconds=env.REDIS_TTL_SECONDS
        )
    return _redis_backend


def reset_redis_backend() -> None:
    """Descarta o backend cacheado (usado nos testes)."""
    global _redis_backend
    _redis_backend = None


async def check_redis() -> CheckStatus:
    """`PING` no Redis, reusando o `health_check()` do backend de workflows.

    Em produção o `StateManager` roda em `StateMode.REDIS` sem fallback para
    JSON, então Redis fora do ar quebra a tool `multi_step_service` inteira —
    e apenas ela.

    Chamado tanto por `/health/detail` (via `health_registry`) quanto por
    `/health/ready` (via `src/health/state.py::evaluate_readiness`), então a
    métrica de dependência gravada aqui (`mcp.dependency.*`, ver
    `src/observability/metrics.py`) cobre os dois caminhos sem duplicação.
    Também é registrada como falha quando a chamada é cancelada por
    `asyncio.wait_for` (timeout do chamador): `BaseException` inclui
    `asyncio.CancelledError`, que não herda de `Exception`.
    """
    backend = _get_redis_backend()
    started = time.monotonic()
    try:
        healthy = await backend.health_check()
    except BaseException:
        metrics.record_dependency_call(
            "redis", success=False, duration_s=time.monotonic() - started
        )
        raise

    duration_s = time.monotonic() - started
    if healthy:
        metrics.record_dependency_call("redis", success=True, duration_s=duration_s)
        return CheckStatus.UP

    metrics.record_dependency_call("redis", success=False, duration_s=duration_s)
    raise HealthCheckError("ping sem resposta")


async def check_bigquery() -> CheckStatus:
    """Valida credencial e alcance da API do BigQuery com um `dry_run`.

    `dry_run` faz o planejamento da query no servidor e volta sem executar
    nada: custo zero, sem consumo de quota de slots, mas exercitando o mesmo
    caminho de autenticação das queries reais.
    """
    from google.cloud import bigquery

    from src.utils.bigquery import get_bigquery_client

    def _probe() -> None:
        client = get_bigquery_client()
        client.query(
            "SELECT 1",
            job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
        )

    # O cliente do BigQuery é síncrono; vai para o executor para não bloquear
    # o event loop. Um `wait_for` que estoure cancela a espera, não a thread —
    # aceitável, já que o próprio cliente tem timeout interno.
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _probe)
    return CheckStatus.UP


async def check_keycloak_jwks() -> CheckStatus:
    """Confirma que o JWKS do Keycloak está acessível.

    Inacessível ⇒ só a autenticação via JWT para; o `HybridTokenVerifier` cai
    para os tokens estáticos de `VALID_TOKENS`.
    """
    from src.config import env

    if not (env.KEYCLOAK_JWKS_URI and env.KEYCLOAK_ISSUER):
        return CheckStatus.SKIPPED

    # httpx puro em vez do `InterceptedHTTPClient`: um probe que falha a cada
    # 30s não deve inundar o error interceptor.
    import httpx

    async with httpx.AsyncClient(timeout=2.0) as client:
        response = await client.get(env.KEYCLOAK_JWKS_URI)

    if response.status_code != 200:
        raise HealthCheckError(f"jwks respondeu HTTP {response.status_code}")
    return CheckStatus.UP


async def check_gcp_credentials() -> CheckStatus:
    """Revalida a service account localmente (sem rede)."""
    if preflight.check_gcp_credentials():
        raise HealthCheckError("service account inválida")
    return CheckStatus.UP


async def check_data_files() -> CheckStatus:
    """Confirma que os arquivos geográficos seguem presentes e legíveis."""
    errors = preflight.check_data_files()
    if errors:
        raise HealthCheckError(f"{len(errors)} arquivo(s) de dados indisponível(is)")
    return CheckStatus.UP


async def check_external_tables() -> CheckStatus:
    """Reporta o veredito da sonda de tabelas externas — sem sondar aqui.

    A sondagem roda num laço de background (`src/health/external_tables.py`)
    porque exige query real (o `dry_run` de `check_bigquery` não detecta
    "Spreadsheet not found") e custa ~2s, acima do que a rodada de
    `/health/detail` comporta.
    """
    results = external_tables.last_result()
    if results is None:
        return CheckStatus.SKIPPED  # sonda ainda não completou o primeiro ciclo

    failed = [table for table, error in results.items() if error is not None]
    if failed:
        # Só o nome curto da tabela: o texto do BigQuery embute o ID da
        # planilha, e `/health/detail` é público.
        names = ", ".join(table.rsplit(".", 1)[-1] for table in sorted(failed))
        raise HealthCheckError(f"tabela(s) externa(s) indisponível(is): {names}")
    return CheckStatus.UP


def make_tool_registry_check(mcp: Any):
    """Cria o check que confirma haver tools registradas.

    Pega o caso em que `EXCLUDED_TOOLS` é configurado errado e esvazia o
    servidor: ele sobe, responde 200 em tudo, e não expõe nenhuma tool.
    """

    async def check_tool_registry() -> CheckStatus:
        if hasattr(mcp, "get_tools"):  # fastmcp
            tools = await mcp.get_tools()
        else:  # mcp.server.fastmcp, usado quando IS_LOCAL
            tools = await mcp.list_tools()

        if not tools:
            raise HealthCheckError("nenhuma tool registrada")
        return CheckStatus.UP

    return check_tool_registry


def register_default_checks(
    mcp: Any, registry: HealthRegistry = health_registry
) -> None:
    """Registra os checks conforme o ambiente."""
    from src.config import env

    registry.register("gcp_credentials", check_gcp_credentials, timeout_s=1.0)
    registry.register("data_files", check_data_files, timeout_s=1.0)
    registry.register("tool_registry", make_tool_registry_check(mcp), timeout_s=1.0)

    if not env.IS_LOCAL:
        # Redis só é usado como backend de estado fora do ambiente local
        # (ver src/tools/langgraph_workflows.py).
        registry.register("redis", check_redis, timeout_s=2.0, critical=True)
        registry.register("bigquery", check_bigquery, timeout_s=2.0, critical=True)
        registry.register("keycloak_jwks", check_keycloak_jwks, timeout_s=2.0)
        # Lê o veredito da sonda de background; não faz I/O, daí o timeout
        # curto. Não é `critical`: a queda afeta só `equipments_instructions`,
        # que degrada graciosamente.
        registry.register("external_tables", check_external_tables, timeout_s=1.0)

    logger.info(f"Health checks registrados: {', '.join(registry.names)}")
