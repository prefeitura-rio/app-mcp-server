"""Falha técnica de tool: o sinal que o Salesforce reconhece como erro (CHATR-234).

O Apex do Agentforce só trata uma chamada como falha quando o resultado MCP vem com
`isError: true`. Uma falha devolvida como dict normal (`success: False`) chega como
sucesso e não aparece em `ACTION_FAILED`. Levantar `ToolError` é o jeito de produzir
`isError: true`: o FastMCP repassa a mensagem intacta em `content`, sem o prefixo
"Error calling tool" que ele põe nas demais exceções e sem `structuredContent`.

A mensagem passa a ser a única coisa que o agente recebe da tool. Por isso ela tem de
ser curta, sem PII, sem stack e sem detalhe técnico.
"""

from typing import NoReturn

from src.config import env

# Marca a exceção como já reportada. Quem chama `falha_tecnica` é responsável pelo
# reporte ao error interceptor (com o contexto que só ele tem); o decorator
# `@interceptor` pula exceções com esta marca para não reportar a mesma falha duas
# vezes.
JA_REPORTADA = "_mcp_falha_tecnica_ja_reportada"


def tool_error_class() -> type[Exception]:
    """`ToolError` da mesma árvore de imports usada em `src/app.py`.

    `IS_LOCAL` troca `fastmcp` por `mcp.server.fastmcp`, e cada árvore tem a sua
    própria classe. Levantar a classe da árvore errada vira exceção genérica, com
    prefixo e texto diferentes entre local e CI.
    """
    if env.IS_LOCAL:
        from mcp.server.fastmcp.exceptions import ToolError
    else:
        from fastmcp.exceptions import ToolError
    return ToolError


def falha_tecnica(mensagem: str) -> NoReturn:
    """Levanta a falha técnica que vira `isError: true` no resultado da tool.

    Use quando a tool não conseguiu fazer o trabalho por falha nossa ou de um
    serviço (timeout, indisponibilidade, resposta truncada). Resultado de negócio
    vazio ("não há") continua sendo resultado normal.
    """
    erro = tool_error_class()(mensagem)
    setattr(erro, JA_REPORTADA, True)
    raise erro
