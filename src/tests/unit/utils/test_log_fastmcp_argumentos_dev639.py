"""
Testes da DEV-639: a falha de validação de argumentos de uma tool não pode levar
ao log o valor que o cliente mandou.

O FastMCP loga pelo `logging` da stdlib (fora da barreira do loguru) a linha
`Invalid arguments for tool %r: %s` com a lista de `errors()` do pydantic, e
cada erro carrega em `input` o valor recebido. O teste ponta a ponta roda o
FastMCP de verdade, com um `Client` em memória; os demais fixam os ramos do
filtro. Todos os valores são sintéticos.
"""

import logging

import pytest
from fastmcp import Client, FastMCP

import src.utils.log as log_mod


INSCRICAO_SINTETICA = "90887766"
NUMERO_SINTETICO = "abc999"
CPF_SINTETICO = "529.982.247-25"


class _Coletor(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.linhas: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.linhas.append(record.getMessage())


@pytest.fixture
def linhas_fastmcp():
    """Handler no próprio logger de origem: recebe o record depois do filtro."""
    log_mod.instalar_filtro_fastmcp()
    alvo = logging.getLogger(log_mod._LOGGER_FASTMCP_SERVER)
    coletor = _Coletor()
    nivel_anterior = alvo.level
    alvo.addHandler(coletor)
    alvo.setLevel(logging.DEBUG)
    try:
        yield coletor.linhas
    finally:
        alvo.removeHandler(coletor)
        alvo.setLevel(nivel_anterior)


def _record(msg, args) -> logging.LogRecord:
    return logging.LogRecord(
        log_mod._LOGGER_FASTMCP_SERVER, logging.WARNING, __file__, 1, msg, args, None
    )


def _filtrar(record: logging.LogRecord) -> str:
    assert log_mod.FiltroArgumentosInvalidosFastMCP().filter(record) is True
    return record.getMessage()


@pytest.mark.asyncio
async def test_chamada_com_argumento_invalido_nao_loga_o_valor(linhas_fastmcp):
    servidor = FastMCP("dev639")

    @servidor.tool
    def consulta(payload_json: str, quantidade: int = 0) -> str:
        return "ok"

    async with Client(servidor) as cliente:
        resultado = await cliente.call_tool(
            "consulta",
            {"payload": INSCRICAO_SINTETICA, "quantidade": NUMERO_SINTETICO},
            raise_on_error=False,
        )
    assert resultado.is_error

    linhas = [linha for linha in linhas_fastmcp if "Invalid arguments" in linha]
    assert len(linhas) == 1
    linha = linhas[0]
    assert INSCRICAO_SINTETICA not in linha
    assert NUMERO_SINTETICO not in linha
    assert "'input'" not in linha
    assert "'consulta'" in linha
    for tipo, campo in (
        ("missing_argument", "payload_json"),
        ("int_parsing", "quantidade"),
        ("unexpected_keyword_argument", "payload"),
    ):
        assert f"{{'type': '{tipo}', 'loc': ('{campo}',)}}" in linha


def test_detalhe_que_nao_e_lista_vira_marcador():
    texto = f"3 validation errors [input_value={{'payload': '{INSCRICAO_SINTETICA}'}}]"
    linha = _filtrar(_record("Invalid arguments for tool %r: %s", ("consulta", texto)))
    assert INSCRICAO_SINTETICA not in linha
    assert linha == "Invalid arguments for tool 'consulta': [detalhe omitido: str]"


def test_erro_fora_do_formato_do_pydantic_nao_vaza():
    detalhe = [INSCRICAO_SINTETICA, {"type": "x", "loc": "campo", "input": "y"}]
    linha = _filtrar(
        _record("Invalid arguments for tool %r: %s", ("consulta", detalhe))
    )
    assert INSCRICAO_SINTETICA not in linha
    assert "'y'" not in linha
    assert "[detalhe omitido: str]" in linha
    assert "{'type': 'x', 'loc': ('campo',)}" in linha


def test_indice_de_lista_no_loc_e_preservado():
    detalhe = [{"type": "int_parsing", "loc": ("itens", 2), "input": "z"}]
    linha = _filtrar(
        _record("Invalid arguments for tool %r: %s", ("consulta", detalhe))
    )
    assert "('itens', 2)" in linha


def test_nome_de_campo_com_formato_de_pii_e_redigido():
    detalhe = [{"type": "unexpected_keyword_argument", "loc": (CPF_SINTETICO,)}]
    linha = _filtrar(
        _record("Invalid arguments for tool %r: %s", ("consulta", detalhe))
    )
    assert CPF_SINTETICO not in linha
    assert "529" not in linha


def test_argumentos_em_formato_inesperado_viram_marcador():
    linha = _filtrar(_record("Invalid arguments for tool", None))
    assert linha == "Invalid arguments for tool: [detalhe omitido: NoneType]"


def test_falha_do_resumo_fecha_sem_o_original(monkeypatch):
    def quebra(_detalhe):
        raise RuntimeError(INSCRICAO_SINTETICA)

    monkeypatch.setattr(log_mod, "_resumir_erros_de_validacao", quebra)
    detalhe = [{"type": "x", "loc": ("payload",), "input": INSCRICAO_SINTETICA}]
    linha = _filtrar(
        _record("Invalid arguments for tool %r: %s", ("consulta", detalhe))
    )
    assert INSCRICAO_SINTETICA not in linha
    assert "[REDACAO-FALHOU:RuntimeError]" in linha


def test_outras_linhas_do_fastmcp_passam_intactas():
    record = _record("Error calling tool %r", ("consulta",))
    assert _filtrar(record) == "Error calling tool 'consulta'"


def test_instalacao_e_idempotente():
    alvo = logging.getLogger(log_mod._LOGGER_FASTMCP_SERVER)
    log_mod.instalar_filtro_fastmcp()
    log_mod.instalar_filtro_fastmcp()
    filtros = [
        f
        for f in alvo.filters
        if type(f).__name__ == log_mod.FiltroArgumentosInvalidosFastMCP.__name__
    ]
    assert len(filtros) == 1
