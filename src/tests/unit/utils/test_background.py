"""Disparo de trabalho em background sem perder a task para o coletor de lixo.

O que estes testes prendem é uma perda de dado que nenhuma das outras redes do
CHATR-103 alcança. Agrupamento, retry, dead-letter e flush no encerramento só
entram em ação depois que a corrotina começa a rodar — e uma task criada com
`asyncio.create_task` e não guardada em lugar nenhum pode ser coletada antes
disso, porque a event loop mantém dela apenas referência fraca. O registro
sumiria sem erro, sem log e sem rastro, um passo antes de toda a proteção.
"""

import asyncio
import gc

import pytest

from src.utils import background as background_module
from src.utils.background import disparar_em_background, tarefas_em_voo


class _LoggerFalso:
    """Captura o que o helper registraria, sem depender do logger real."""

    def __init__(self):
        self.erros = []

    def error(self, msg, **_kwargs):
        self.erros.append(msg)


@pytest.mark.asyncio
async def test_task_sobrevive_a_uma_coleta_de_lixo():
    """O caso real: o chamador descarta o retorno e o GC roda antes da task."""
    executou = asyncio.Event()

    async def trabalho():
        await asyncio.sleep(0)
        executou.set()

    disparar_em_background(trabalho(), nome="teste:sobrevive")
    # Sem a referência forte do módulo, é aqui que a task some.
    gc.collect()

    await asyncio.wait_for(executou.wait(), timeout=1.0)
    assert executou.is_set()


@pytest.mark.asyncio
async def test_referencia_e_solta_ao_terminar():
    """Segurar a task para sempre trocaria perda de dado por vazamento de memória."""
    antes = tarefas_em_voo()

    async def trabalho():
        await asyncio.sleep(0)

    task = disparar_em_background(trabalho(), nome="teste:solta")
    assert tarefas_em_voo() == antes + 1

    await task
    # O `discard` roda no done-callback, agendado na volta ao loop.
    await asyncio.sleep(0)
    assert tarefas_em_voo() == antes


@pytest.mark.asyncio
async def test_falha_e_registrada_em_vez_de_sumir(monkeypatch):
    """Sem consumir o resultado, o erro só apareceria no destrutor, sem contexto."""
    logger_falso = _LoggerFalso()
    # Monkeypatch pelo objeto do módulo, e não por caminho em string: outros
    # testes desta suíte substituem `src.utils` em `sys.modules` por um pacote
    # sintético, e a resolução por string cairia nele.
    monkeypatch.setattr(background_module, "logger", logger_falso)

    async def trabalho():
        raise RuntimeError("falha na escrita")

    task = disparar_em_background(trabalho(), nome="teste:falha")
    with pytest.raises(RuntimeError):
        await task
    await asyncio.sleep(0)

    assert logger_falso.erros, "a falha da task de background passou em silêncio"
    assert "teste:falha" in logger_falso.erros[0]


@pytest.mark.asyncio
async def test_cancelamento_nao_e_tratado_como_erro(monkeypatch):
    """Cancelar é o caminho normal de encerramento, não incidente."""
    logger_falso = _LoggerFalso()
    # Monkeypatch pelo objeto do módulo, e não por caminho em string: outros
    # testes desta suíte substituem `src.utils` em `sys.modules` por um pacote
    # sintético, e a resolução por string cairia nele.
    monkeypatch.setattr(background_module, "logger", logger_falso)

    async def trabalho():
        await asyncio.sleep(10)

    task = disparar_em_background(trabalho(), nome="teste:cancelado")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert logger_falso.erros == []
    assert tarefas_em_voo() == 0
