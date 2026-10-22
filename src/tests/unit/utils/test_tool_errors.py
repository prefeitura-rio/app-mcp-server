"""Testes da falha técnica de tool (CHATR-234) e dos defaults da busca (CHATR-298).

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
# Configuração da busca: a lista de modelos substitui as variáveis avulsas (CHATR-298)
# --------------------------------------------------------------------------------


def test_defaults_da_busca():
    """Sem env no ambiente de teste, vale a configuração de produção do código. Os
    tetos de token, o prazo e as tentativas agora são por modelo (ver
    `src/tests/unit/config/test_gemini_search.py`)."""
    from src.config import gemini_search

    assert env.GEMINI_SEARCH_CONFIG is gemini_search.DEFAULT_CONFIG
    assert env.GEMINI_SEARCH_RETRY_BASE_SECONDS == 2.0
    assert env.GEMINI_SEARCH_RETRY_MAX_BACKOFF_SECONDS == 16.0
    for aposentada in (
        "GEMINI_MODEL",
        "GEMINI_SEARCH_MODELS",
        "GEMINI_SEARCH_DEADLINE_SECONDS",
        "GEMINI_SEARCH_MODEL_DEADLINE_SECONDS",
        "GEMINI_SEARCH_ATTEMPTS_PER_MODEL",
        "GEMINI_SEARCH_RETRY_ATTEMPTS",
        "GEMINI_SEARCH_RETRY_BUDGET_SECONDS",
        "GEMINI_SEARCH_THINKING_LEVEL",
        "GEMINI_SEARCH_REASONING_BUDGET_TOKENS",
        "GEMINI_SEARCH_MAX_OUTPUT_TOKENS",
    ):
        assert not hasattr(env, aposentada), aposentada
