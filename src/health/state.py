"""Estado de prontidão do processo, consultado por `/health/ready`.

Separado do registry de propósito: readiness aqui responde "este processo
terminou de inicializar", e nada mais. Ele deliberadamente NÃO reflete a
saúde das dependências — Redis fora do ar quebra apenas a tool
`multi_step_service`, e retornar 503 tiraria o pod do balanceador (com
`replicas: 1` em produção, isso converteria uma falha parcial em
indisponibilidade total).

Sobre o encerramento: `set_ready(False)` é chamado ao sair do lifespan, mas
esse caminho NÃO é percorrido em SIGTERM — a uvicorn restaura o handler
original do sinal e o re-emite ao fim de `serve()`, matando o processo antes
do desenrolar do lifespan. Tirar o pod do balanceador no encerramento é papel
do Kubernetes, que remove o endpoint assim que o pod entra em Terminating.
"""

from __future__ import annotations

import time

_ready = False
_started_at = time.monotonic()


def set_ready(value: bool) -> None:
    """Marca o processo como pronto (fim do startup) ou não (shutdown)."""
    global _ready
    _ready = value


def is_ready() -> bool:
    return _ready


def uptime_seconds() -> int:
    return int(time.monotonic() - _started_at)
