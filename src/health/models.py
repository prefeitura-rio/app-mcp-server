"""Modelos de dados dos health checks."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable


class CheckStatus(str, Enum):
    """Resultado possível de um check individual.

    `SKIPPED` cobre dependências opcionais que não estão configuradas no
    ambiente (ex.: Keycloak sem JWKS_URI) — ausência de configuração não é
    falha e não deve degradar o status agregado.
    """

    UP = "up"
    DOWN = "down"
    SKIPPED = "skipped"


# Status agregados expostos em /health/detail.
STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"


class HealthCheckError(Exception):
    """Falha de check cuja mensagem foi escrita por nós.

    É o único caminho pelo qual texto livre chega ao `/health/detail`: para
    qualquer outra exceção só o nome da classe é exposto, já que mensagens de
    clientes de rede embutem rotineiramente URLs e credenciais.
    """


@dataclass(frozen=True)
class CheckResult:
    """Resultado de um check, já pronto para serialização.

    `error` sempre chega aqui sanitizado (ver `registry.sanitize_error`): a
    resposta é pública, então nunca pode carregar URL, host ou credencial.
    """

    name: str
    status: CheckStatus
    latency_ms: int
    error: str | None = None
    critical: bool = False

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "name": self.name,
            "status": self.status.value,
            "latency_ms": self.latency_ms,
            "critical": self.critical,
        }
        if self.error:
            payload["error"] = self.error
        return payload


def aggregate_status(results: Iterable[CheckResult]) -> str:
    """Reduz os resultados a um status único.

    Qualquer check `DOWN` degrada o agregado. `SKIPPED` não degrada.
    """
    return (
        STATUS_DEGRADED
        if any(r.status is CheckStatus.DOWN for r in results)
        else STATUS_OK
    )
