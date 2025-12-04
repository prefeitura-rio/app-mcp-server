from src.app import mcp
from langchain_core.tools import StructuredTool
from src.config import env
from src.utils.log import logger

# Extract tools from FastMCP and convert to LangChain tools
mcp_tools = []
try:
    if hasattr(mcp, "_tool_manager") and hasattr(mcp._tool_manager, "_tools"):
        # FastMCP stores tools as Tool objects, we need the underlying function
        for tool_name, tool_def in mcp._tool_manager._tools.items():
            if tool_name not in env.MCP_EXCLUDED_TOOLS:

                if hasattr(tool_def, "fn"):
                    # Ensure the function has a docstring (required by LangChain)
                    if not tool_def.fn.__doc__ and hasattr(tool_def, "description"):
                        tool_def.fn.__doc__ = tool_def.description

                    # Convert to StructuredTool to ensure consistency
                    tool = StructuredTool.from_function(
                        tool_def.fn,
                        name=tool_name,
                        description=(
                            tool_def.description
                            if hasattr(tool_def, "description")
                            else None
                        ),
                    )
                    mcp_tools.append(tool)
                else:
                    logger.info(
                        f"⚠️ Warning: Could not extract function for tool {tool_name}"
                    )
            else:
                logger.info(f"Excluded tool: {tool_name}")
        else:
            logger.info("⚠️ Warning: Could not access FastMCP tool manager")
except Exception as e:
    logger.info(f"❌ Error extracting tools: {e}")
