"""Registro e execução dos checks de dependência expostos em `/health/detail`.

Garantias que este módulo oferece ao endpoint que o consome:

- nenhum check pode derrubar a resposta (toda exceção vira `DOWN`);
- nenhum check pode pendurar a resposta (timeout por check + teto global);
- nenhum check pode martelar uma dependência (cache curto + single-flight);
- nenhum check pode vazar credencial na resposta (ver `sanitize_error`).

Não importa `src.config.env`: as variáveis de ajuste são lidas via
`getenv_or_action`, o que mantém o módulo importável isoladamente nos testes
unitários (que constroem um pacote `src` sintético).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from src.health.models import CheckResult, CheckStatus, HealthCheckError
from src.utils.infisical import getenv_or_action
from src.utils.log import logger

CheckCallable = Callable[[], Awaitable[CheckStatus]]


def _env_float(name: str, default: float) -> float:
    raw = getenv_or_action(name, default=str(default), action="ignore")
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning(f"{name} inválido ({raw!r}); usando o padrão de {default}s.")
        return default


# Timeout por check. Precisa ser menor que o `timeoutSeconds` do probe.
DEFAULT_CHECK_TIMEOUT_S = _env_float("HEALTH_CHECK_TIMEOUT_S", 2.0)
# Teto para a rodada inteira. Backstop para um check que bloqueie o event
# loop e por isso escape do próprio `wait_for`.
GLOBAL_TIMEOUT_S = _env_float("HEALTH_GLOBAL_TIMEOUT_S", 3.0)
# Janela em que o resultado é reaproveitado entre requisições.
CACHE_TTL_S = _env_float("HEALTH_CACHE_TTL_S", 10.0)


@dataclass(frozen=True)
class RegisteredCheck:
    name: str
    fn: CheckCallable
    timeout_s: float
    critical: bool


def sanitize_error(exc: BaseException) -> str:
    """Reduz uma exceção ao que pode ser exposto publicamente.

    `/health/detail` não exige autenticação, e mensagens de exceção de
    clientes de rede embutem rotineiramente a URL de conexão — no caso do
    Redis, com a senha dentro. Por isso apenas o nome da classe atravessa; a
    exceção completa vai para o log, onde o operador a alcança.

    A única exceção é `HealthCheckError`, cuja mensagem foi escrita por nós.
    """
    if isinstance(exc, HealthCheckError):
        return str(exc)
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout"
    return type(exc).__name__


class HealthRegistry:
    """Coleção de checks executados em paralelo, com cache e timeouts."""

    def __init__(
        self,
        *,
        cache_ttl_s: float = CACHE_TTL_S,
        global_timeout_s: float = GLOBAL_TIMEOUT_S,
        default_timeout_s: float = DEFAULT_CHECK_TIMEOUT_S,
    ) -> None:
        self._checks: Dict[str, RegisteredCheck] = {}
        self._cache_ttl_s = cache_ttl_s
        self._global_timeout_s = global_timeout_s
        self._default_timeout_s = default_timeout_s
        self._cached: Optional[Tuple[float, List[CheckResult]]] = None
        self._lock = asyncio.Lock()

    def register(
        self,
        name: str,
        fn: CheckCallable,
        *,
        timeout_s: Optional[float] = None,
        critical: bool = False,
    ) -> None:
        """Registra (ou substitui) um check.

        `critical` é puramente informativo no payload: nenhum check tira o
        pod do balanceador — ver `src/health/state.py`.
        """
        self._checks[name] = RegisteredCheck(
            name=name,
            fn=fn,
            timeout_s=timeout_s if timeout_s is not None else self._default_timeout_s,
            critical=critical,
        )

    def clear(self) -> None:
        self._checks.clear()
        self._cached = None

    @property
    def names(self) -> Tuple[str, ...]:
        return tuple(self._checks)

    async def run_all(self, *, force: bool = False) -> List[CheckResult]:
        """Executa todos os checks, reaproveitando o cache dentro do TTL.

        O lock dá single-flight: uma rajada de requisições concorrentes a
        `/health/detail` produz uma única rodada de sondagens.
        """
        async with self._lock:
            if not force and self._cached is not None:
                cached_at, results = self._cached
                if time.monotonic() - cached_at < self._cache_ttl_s:
                    return results

            results = await self._execute()
            self._cached = (time.monotonic(), results)
            return results

    async def _execute(self) -> List[CheckResult]:
        checks = list(self._checks.values())
        if not checks:
            return []

        # `_run_one` nunca levanta `Exception`, então o gather é seguro sem
        # `return_exceptions`; o `wait_for` externo cobre o caso patológico
        # de um check síncrono que bloqueie o event loop.
        try:
            return list(
                await asyncio.wait_for(
                    asyncio.gather(*(self._run_one(check) for check in checks)),
                    timeout=self._global_timeout_s,
                )
            )
        except asyncio.TimeoutError:
            logger.error(
                f"Rodada de health checks excedeu o teto global de "
                f"{self._global_timeout_s}s; reportando todos como indisponíveis."
            )
            return [
                CheckResult(
                    name=check.name,
                    status=CheckStatus.DOWN,
                    latency_ms=int(self._global_timeout_s * 1000),
                    error="timeout",
                    critical=check.critical,
                )
                for check in checks
            ]

    async def _run_one(self, check: RegisteredCheck) -> CheckResult:
        started = time.monotonic()
        try:
            status = await asyncio.wait_for(check.fn(), timeout=check.timeout_s)
        except Exception as exc:  # noqa: BLE001 - um check nunca derruba o endpoint
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.warning(
                f"Health check '{check.name}' falhou após {elapsed_ms}ms: {exc!r}"
            )
            return CheckResult(
                name=check.name,
                status=CheckStatus.DOWN,
                latency_ms=elapsed_ms,
                error=sanitize_error(exc),
                critical=check.critical,
            )

        elapsed_ms = int((time.monotonic() - started) * 1000)
        if not isinstance(status, CheckStatus):
            logger.error(
                f"Health check '{check.name}' retornou {status!r}, "
                f"esperado CheckStatus."
            )
            return CheckResult(
                name=check.name,
                status=CheckStatus.DOWN,
                latency_ms=elapsed_ms,
                error="invalid_check_result",
                critical=check.critical,
            )

        return CheckResult(
            name=check.name,
            status=status,
            latency_ms=elapsed_ms,
            critical=check.critical,
        )


# Registry global usado pela aplicação; os checks são registrados em
# `src/health/checks.py::register_default_checks`.
health_registry = HealthRegistry()
