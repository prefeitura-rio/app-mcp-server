from typing import List, Optional
from langchain_core.tools import BaseTool
import asyncio

from langchain_mcp_adapters.client import MultiServerMCPClient

from src.config import env


async def get_mcp_tools(
    include_tools: Optional[List[str]] = None, exclude_tools: Optional[List[str]] = None
) -> List[BaseTool]:
    """
    Inicializa o cliente MCP e busca as ferramentas disponíveis de forma assíncrona.

    Args:
        include_tools (List[str], optional): Lista de nomes de ferramentas para incluir.
                                           Se fornecida, apenas essas ferramentas serão retornadas.
        exclude_tools (List[str], optional): Lista de nomes de ferramentas para excluir.
                                           Se fornecida, todas as ferramentas exceto essas serão retornadas.

    Returns:
        List[BaseTool]: Lista de ferramentas disponíveis do servidor MCP, filtrada conforme os parâmetros
    """
    # Initialize default values
    if include_tools is None:
        include_tools = []
    if exclude_tools is None:
        exclude_tools = []

    client = MultiServerMCPClient(
        {
            "rio_mcp": {
                "transport": "streamable_http",
                "url": env.MPC_SERVER_URL,
                "headers": {
                    "Authorization": f"Bearer {env.MPC_API_TOKEN}",
                },
            },
        }
    )
    tools = await client.get_tools()

    # Apply filtering logic
    if include_tools:
        # If include list is not empty, return only tools in the include list
        filtered_tools = [tool for tool in tools if tool.name in include_tools]
    elif exclude_tools:
        # If exclude list is not empty, return all tools except the ones in exclude list
        filtered_tools = [tool for tool in tools if tool.name not in exclude_tools]
    else:
        # If both lists are empty, return all tools
        filtered_tools = tools

    return filtered_tools


mcp_tools = asyncio.run(get_mcp_tools(exclude_tools=env.MCP_EXCLUDED_TOOLS))
