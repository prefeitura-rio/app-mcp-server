"""Testes da falha técnica de tool e dos tetos da busca (CHATR-234).

A falha técnica precisa sair como `isError: true` com a mensagem intacta — é o único
sinal que o Apex do Salesforce trata como falha — e não pode ser reportada duas vezes
ao error interceptor.
"""

import pytest
from fastmcp import Client, FastMCP

from src.config import env
from src.utils import error_interceptor, tool_errors

MENSAGEM = "Não foi possível concluir a busca na web neste momento."


@pytest.mark.parametrize(
    ("is_local", "modulo"),
    [(False, "fastmcp.exceptions"), (True, "mcp.server.fastmcp.exceptions")],
)
def test_tool_error_segue_a_arvore_de_imports(monkeypatch, is_local, modulo):
    """Mesma árvore de `src/app.py`: `IS_LOCAL` troca `fastmcp` pelo SDK oficial."""
    monkeypatch.setattr(tool_errors.env, "IS_LOCAL", is_local)

    classe = tool_errors.tool_error_class()

    assert classe.__name__ == "ToolError"
    assert classe.__module__ == modulo


def test_falha_tecnica_levanta_marcada_como_reportada(monkeypatch):
    monkeypatch.setattr(tool_errors.env, "IS_LOCAL", False)

    with pytest.raises(tool_errors.tool_error_class()) as excinfo:
        tool_errors.falha_tecnica(MENSAGEM)

    assert str(excinfo.value) == MENSAGEM
    assert getattr(excinfo.value, tool_errors.JA_REPORTADA) is True


@pytest.mark.asyncio
async def test_falha_tecnica_vira_is_error_com_a_mensagem_intacta(monkeypatch):
    """Árvore do CI (`fastmcp`): sem o prefixo "Error calling tool" e sem
    `structuredContent`."""
    monkeypatch.setattr(tool_errors.env, "IS_LOCAL", False)
    mcp = FastMCP("teste")

    @mcp.tool
    async def busca(query: str) -> dict:
        tool_errors.falha_tecnica(MENSAGEM)

    async with Client(mcp) as client:
        result = await client.call_tool("busca", {"query": "x"}, raise_on_error=False)

    assert result.is_error is True
    assert [item.text for item in result.content] == [MENSAGEM]
    assert result.structured_content is None


@pytest.mark.asyncio
async def test_interceptor_nao_reporta_falha_tecnica_de_novo(
    monkeypatch, block_real_error_interceptor
):
    monkeypatch.setattr(tool_errors.env, "IS_LOCAL", False)

    @error_interceptor.interceptor(source={"source": "mcp", "tool": "teste"})
    async def tool_que_falha():
        tool_errors.falha_tecnica(MENSAGEM)

    with pytest.raises(tool_errors.tool_error_class()):
        await tool_que_falha()

    block_real_error_interceptor.assert_not_called()


@pytest.mark.asyncio
async def test_interceptor_continua_reportando_outras_excecoes(
    block_real_error_interceptor,
):
    @error_interceptor.interceptor(source={"source": "mcp", "tool": "teste"})
    async def tool_quebrada():
        raise ValueError("bug")

    with pytest.raises(ValueError):
        await tool_quebrada()

    block_real_error_interceptor.assert_called_once()


def test_interceptor_sync_nao_reporta_falha_tecnica(
    monkeypatch, block_real_error_interceptor
):
    monkeypatch.setattr(tool_errors.env, "IS_LOCAL", False)

    @error_interceptor.interceptor(source={"source": "mcp", "tool": "teste"})
    def tool_sync_que_falha():
        tool_errors.falha_tecnica(MENSAGEM)

    with pytest.raises(tool_errors.tool_error_class()):
        tool_sync_que_falha()

    block_real_error_interceptor.assert_not_called()


# --------------------------------------------------------------------------------
# Tetos de token da busca: o default seguro mora no env, nunca "sem limite"
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        pytest.param(None, 8192, id="ausente"),
        pytest.param("4096", 4096, id="valido"),
        pytest.param("0", 0, id="zero_desliga_o_raciocinio"),
        pytest.param("-1", 8192, id="automatico_do_sdk_rejeitado"),
        pytest.param("-50", 8192, id="negativo"),
        pytest.param("muito", 8192, id="nao_inteiro"),
        pytest.param(" 2048 ", 2048, id="com_espacos"),
    ],
)
def test_budget_de_raciocinio(monkeypatch, valor, esperado):
    nome = "TESTE_CHATR234_REASONING_BUDGET"
    if valor is None:
        monkeypatch.delenv(nome, raising=False)
    else:
        monkeypatch.setenv(nome, valor)

    assert env._tokens_or_default(nome, default=8192, minimum=0) == esperado


def test_max_output_nao_aceita_zero(monkeypatch):
    nome = "TESTE_CHATR234_MAX_OUTPUT"
    monkeypatch.setenv(nome, "0")

    assert env._tokens_or_default(nome, default=12288, minimum=1) == 12288


def test_defaults_da_busca():
    """Sem env no ambiente de teste, valem os defaults calibrados no CHATR-234."""
    assert env.GEMINI_SEARCH_REASONING_BUDGET_TOKENS == 8192
    assert env.GEMINI_SEARCH_MAX_OUTPUT_TOKENS == 12288
    assert env.GEMINI_SEARCH_DEADLINE_SECONDS == 45.0
    assert not hasattr(env, "GEMINI_SEARCH_RETRY_BUDGET_SECONDS")
