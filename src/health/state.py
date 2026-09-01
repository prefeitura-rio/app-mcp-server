"""Estado de prontidão do processo, consultado por `/health/ready`.

Readiness combina dois sinais:

1. `is_ready()`: flag trivial de processo — "o startup terminou?". Sem I/O.
2. `evaluate_readiness()`: o mesmo flag, mais uma checagem LIMITADA no tempo
   da única dependência sem fallback em produção — o Redis (ver
   `src/tools/multi_step_service/core/state.py`: `StateMode.REDIS` roda sem
   fallback para JSON). Nenhuma outra dependência gateia tráfego: BigQuery,
   Keycloak JWKS, arquivos de dados e tabelas externas de Sheets continuam
   só em `/health/detail` (`src/health/checks.py`) porque sua queda degrada
   uma tool específica, não o processo inteiro.

Por que isto mudou (Task 7 do plano de resiliência do MCP): até a Task 4,
produção rodava com `replicas: 1`, e qualquer readiness que dependesse de
Redis equivalia a converter uma falha parcial em indisponibilidade total —
por isso o desenho anterior era deliberadamente cego a dependências. A
Task 4 (`k8s/prod/resources.yaml`) trouxe HPA (`minReplicas: 3`) e PDB
(`minAvailable: 2`), e as Tasks 5/6 trouxeram Redis Sentinel HA e retry
limitado no cliente. Com múltiplas réplicas espalhadas por zona/host, tirar
APENAS o pod que não alcança o Redis do balanceador passa a ser o
comportamento correto — o tráfego é desviado para réplicas saudáveis em vez
de continuar sendo enviado (e falhando) para uma que não pode servir
`multi_step_service`.

Explicitamente FORA do escopo de readiness, por construção: exportação de
telemetria (OTel/coletor). `evaluate_readiness()` não importa
`src.observability.tracing` nem qualquer exportador OTLP — a leitura fica
independente da disponibilidade do coletor SigNoz, com ou sem tracing/
métricas habilitados.

Sobre o encerramento: `set_ready(False)` é chamado ao sair do lifespan, mas
esse caminho NÃO é percorrido em SIGTERM — a uvicorn restaura o handler
original do sinal e o re-emite ao fim de `serve()`, matando o processo antes
do desenrolar do lifespan. Tirar o pod do balanceador no encerramento é papel
do Kubernetes, que remove o endpoint assim que o pod entra em Terminating.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional, Tuple

from src.utils.infisical import getenv_or_action
from src.utils.log import logger

_ready = False
_started_at = time.monotonic()


def _env_float(name: str, default: float) -> float:
    raw = getenv_or_action(name, default=str(default), action="ignore")
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning(f"{name} inválido ({raw!r}); usando o padrão de {default}s.")
        return default


# Teto para a checagem de Redis feita a partir de /health/ready. Precisa ser
# menor que o `timeoutSeconds` do readinessProbe (5s em ambos os manifestos)
# com folga para o round-trip HTTP: um probe que estoura o próprio timeout
# vira "unknown" para o kubelet, não "not ready" — pior para diagnóstico.
READINESS_REDIS_TIMEOUT_S = _env_float("READINESS_REDIS_TIMEOUT_S", 2.0)

# Motivos possíveis em `/health/ready` — conjunto fechado e não sensível.
REASON_STARTING = "starting"
REASON_REDIS_UNAVAILABLE = "redis_unavailable"


def set_ready(value: bool) -> None:
    """Marca o processo como pronto (fim do startup) ou não (shutdown)."""
    global _ready
    _ready = value


def is_ready() -> bool:
    """Prontidão de processo, sem I/O — usada por `checks.py`/diagnóstico."""
    return _ready


def uptime_seconds() -> int:
    return int(time.monotonic() - _started_at)


async def _redis_reachable() -> bool:
    """Sonda o Redis com teto de tempo, reaproveitando o backend compartilhado.

    Importa `src.config.env` e `src.health.checks` dentro da função (e não no
    topo do módulo) para manter `state.py` importável em isolamento nos
    testes unitários, seguindo a mesma convenção de `checks.py`/`registry.py`.

    Ambiente local não usa Redis como backend de estado (ver
    `src/health/checks.py::register_default_checks`), então não há o que
    sondar — retorna pronto sem I/O.
    """
    from src.config import env

    if env.IS_LOCAL:
        return True

    from src.health.checks import check_redis

    try:
        await asyncio.wait_for(check_redis(), timeout=READINESS_REDIS_TIMEOUT_S)
        return True
    except Exception:
        # Qualquer falha (timeout, `HealthCheckError`, `ConnectionError`, ...)
        # vira "não pronto": o corpo da resposta não expõe qual foi (ver
        # `evaluate_readiness`), e o detalhe completo, sanitizado, já é
        # responsabilidade de `/health/detail`.
        return False


async def evaluate_readiness() -> Tuple[bool, Optional[str]]:
    """Decide o status de `/health/ready`: `(pronto, motivo_se_nao_pronto)`.

    Não depende de telemetria: nenhum caminho aqui toca
    `src.observability.tracing`/métricas ou qualquer exportador OTLP, então
    um coletor OTel indisponível nunca afeta o resultado.
    """
    if not is_ready():
        return False, REASON_STARTING

    if not await _redis_reachable():
        return False, REASON_REDIS_UNAVAILABLE

    return True, None
