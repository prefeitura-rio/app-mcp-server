"""Disparo de trabalho em background sem perder a task para o coletor de lixo.

`asyncio.create_task` devolve a task, mas a event loop guarda apenas uma
referência **fraca** para ela. Uma task criada e não guardada em lugar nenhum
pode ser coletada antes de terminar — e some sem erro, sem log e sem rastro.

Isso importa aqui mais do que na média dos projetos: as tasks disparadas assim
são justamente as escritas de log, feedback e alerta no BigQuery. Todo o
trabalho do CHATR-103 (agrupamento, retry, dead-letter, flush no encerramento)
existe para que um registro não se perca em silêncio — e nada disso é acionado
se a corrotina nunca chega a rodar. Seria a mesma perda, um passo antes.

O padrão já está documentado em `src/app.py`, onde as tasks do lifespan são
mantidas vivas em variável local pelo mesmo motivo. Este módulo é a versão
reutilizável dele, para os call sites que não têm um escopo longo onde segurar
a referência.
"""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine, Optional, Set

from src.utils.log import logger

# Referência forte enquanto a task vive. O `discard` no done-callback é o que
# impede este conjunto de virar vazamento de memória: cada task sai daqui assim
# que termina, com ou sem erro.
_tarefas_vivas: Set[asyncio.Task] = set()


def _ao_terminar(task: asyncio.Task) -> None:
    """Solta a referência e registra falha que ninguém mais veria.

    Sem consumir o resultado, uma exceção numa task fire-and-forget só apareceria
    no destrutor, como "Task exception was never retrieved" — mensagem sem
    contexto e fora de ordem. `CancelledError` é encerramento normal e não é
    tratado como erro.
    """
    _tarefas_vivas.discard(task)
    if task.cancelled():
        return
    excecao = task.exception()
    if excecao is not None:
        logger.error(
            f"Tarefa de background '{task.get_name()}' falhou: {excecao!r}",
            exc_info=excecao,
        )


def disparar_em_background(
    coro: Coroutine[Any, Any, Any], *, nome: Optional[str] = None
) -> asyncio.Task:
    """Agenda `coro` na event loop mantendo a task viva até o fim.

    Use no lugar de `asyncio.create_task` sempre que o resultado não for
    aguardado — que é o caso de toda escrita de log/feedback/alerta.

    Args:
        coro: corrotina a executar.
        nome: nome da task, usado no log de falha e em introspecção.

    Returns:
        asyncio.Task: a task agendada (normalmente descartada pelo chamador).
    """
    task = asyncio.create_task(coro, name=nome)
    _tarefas_vivas.add(task)
    task.add_done_callback(_ao_terminar)
    return task


def tarefas_em_voo() -> int:
    """Quantas tasks de background ainda não terminaram. Existe para teste."""
    return len(_tarefas_vivas)


__all__ = ["disparar_em_background", "tarefas_em_voo"]
