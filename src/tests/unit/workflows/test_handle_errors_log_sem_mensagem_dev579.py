"""
Testes da DEV-579: o decorator `handle_errors` do núcleo do multi_step_service
não leva a mensagem da exceção para o log.

Antes, o `logger.error` do decorator imprimia `traceback.format_exc()`, cuja
última linha é `str(exceção)`. Exceção de domínio embute o valor que a
provocou (`InvalidInscricaoError` leva a inscrição imobiliária), e inscrição,
guia e CDA não têm formato nem rótulo que a barreira de `src/utils/log.py`
reconheça: o valor saía em claro, por qualquer nó de qualquer workflow.

Invariantes cobertos aqui:
- pelo decorator real, com a barreira de log instalada, nenhuma linha de log
  (stderr do sink de produção e sink de teste) contém o valor embutido na
  exceção, nem nas exceções encadeadas (`from` e contexto implícito);
- o log continua localizando a falha: serviço, nó, tipo de cada exceção da
  cadeia e o frame do nó que estourou;
- a resposta ao agente não muda: `status = "error"`, `error_message = str(e)` e
  a `AgentResponse` que o nó já tinha preparado é preservada.

Dados sintéticos.
"""

import pytest
from loguru import logger

import src.utils.log as log_mod
from src.tools.multi_step_service.core.base_workflow import (
    _traceback_sem_mensagem,
    handle_errors,
)
from src.tools.multi_step_service.core.models import AgentResponse, ServiceState
from src.tools.multi_step_service.workflows.iptu_pagamento.api.exceptions import (
    InvalidInscricaoError,
)

INSCRICAO = "87654321"
GUIA = "4455667788"
CDA = "990011223344"
TRECHOS_PROIBIDOS = (INSCRICAO, GUIA, CDA, "Inscrição imobiliária", "não encontrada")


class _ErroDeDominio(Exception):
    pass


@pytest.fixture(autouse=True)
def barreira_de_log():
    """Barreira deste objeto de módulo ativa, como em `test_log_redaction.py`."""
    log_mod._redacao_instalada = False
    log_mod.instalar_redacao()
    yield


@pytest.fixture
def capturado():
    registros = []
    sink_id = logger.add(registros.append, level="DEBUG", format="{message}")
    try:
        yield registros
    finally:
        logger.remove(sink_id)


def _sem_valor(texto: str) -> None:
    for trecho in TRECHOS_PROIBIDOS:
        assert trecho not in texto, f"valor da exceção no log: {trecho!r}"


def _estado() -> ServiceState:
    return ServiceState(user_id="5521999999999", service_name="iptu_pagamento")


class _No:
    @handle_errors
    async def informar_inscricao(self, state: ServiceState) -> ServiceState:
        state.agent_response = AgentResponse(description="Informe a inscrição.")
        raise InvalidInscricaoError(inscricao=INSCRICAO)

    @handle_errors
    async def emitir_guia(self, state: ServiceState) -> ServiceState:
        try:
            raise ValueError(f"CDA {CDA} sem débito")
        except ValueError as origem:
            raise _ErroDeDominio(f"guia {GUIA} não emitida") from origem

    @handle_errors
    async def consultar(self, state: ServiceState) -> ServiceState:
        try:
            raise KeyError(f"guia {GUIA}")
        except KeyError:
            raise _ErroDeDominio(f"inscrição {INSCRICAO}")

    @handle_errors
    async def sem_contexto(self, state: ServiceState) -> ServiceState:
        try:
            raise KeyError(f"guia {GUIA}")
        except KeyError:
            raise _ErroDeDominio(f"inscrição {INSCRICAO}") from None


@pytest.mark.asyncio
async def test_inscricao_da_excecao_nao_vai_para_o_log(capsys, capturado):
    state = await _No().informar_inscricao(_estado())

    stderr = capsys.readouterr().err
    texto = stderr + "".join(str(r) for r in capturado)
    assert "Error in service: iptu_pagamento" in stderr
    _sem_valor(texto)
    assert "node:informar_inscricao" in stderr
    assert "InvalidInscricaoError" in stderr
    assert "in informar_inscricao" in stderr
    assert "user_id:[REDACTED" in stderr
    assert "999999999" not in texto
    assert state.status == "error"


@pytest.mark.asyncio
async def test_resposta_ao_agente_nao_muda(capsys):
    state = await _No().informar_inscricao(_estado())

    assert state.status == "error"
    assert state.agent_response.description == "Informe a inscrição."
    assert state.agent_response.error_message == str(
        InvalidInscricaoError(inscricao=INSCRICAO)
    )


@pytest.mark.asyncio
async def test_excecao_encadeada_com_from_nao_vaza_nenhuma_mensagem(capsys, capturado):
    state = await _No().emitir_guia(_estado())

    stderr = capsys.readouterr().err
    _sem_valor(stderr + "".join(str(r) for r in capturado))
    assert "builtins.ValueError" in stderr
    assert "_ErroDeDominio" in stderr
    assert "direct cause" in stderr
    assert stderr.index("builtins.ValueError") < stderr.index("_ErroDeDominio\n")
    assert state.agent_response.error_message == f"guia {GUIA} não emitida"


@pytest.mark.asyncio
async def test_contexto_implicito_nao_vaza_nenhuma_mensagem(capsys, capturado):
    await _No().consultar(_estado())

    stderr = capsys.readouterr().err
    _sem_valor(stderr + "".join(str(r) for r in capturado))
    assert "builtins.KeyError" in stderr
    assert "During handling of the above exception" in stderr


@pytest.mark.asyncio
async def test_from_none_omite_o_contexto(capsys):
    await _No().sem_contexto(_estado())

    stderr = capsys.readouterr().err
    _sem_valor(stderr)
    assert "KeyError" not in stderr
    assert "During handling" not in stderr
    assert "_ErroDeDominio" in stderr


def test_traceback_sem_mensagem_sem_traceback_anexado():
    texto = _traceback_sem_mensagem(_ErroDeDominio(f"inscrição {INSCRICAO}"))

    assert texto == (
        "Traceback (most recent call last):\n"
        f"{_ErroDeDominio.__module__}._ErroDeDominio\n"
    )


def test_traceback_sem_mensagem_tolera_ciclo_na_cadeia():
    a = _ErroDeDominio(f"guia {GUIA}")
    b = ValueError(f"CDA {CDA}")
    a.__cause__ = b
    b.__cause__ = a

    texto = _traceback_sem_mensagem(a)

    _sem_valor(texto)
    assert "builtins.ValueError" in texto
    assert texto.count("Traceback (most recent call last):") == 2
