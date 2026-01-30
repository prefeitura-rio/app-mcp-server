from src.app import mcp
from langchain_core.tools import StructuredTool
from src.config import env
from src.utils.log import logger
import inspect

# Extract tools from FastMCP and convert to LangChain tools
mcp_tools = []
try:
    if hasattr(mcp, "_tool_manager") and hasattr(mcp._tool_manager, "_tools"):
        # FastMCP stores tools as Tool objects, we need the underlying function
        for tool_name, tool_def in mcp._tool_manager._tools.items():
            if tool_name not in env.EXCLUDED_TOOLS:
                # Use getattr to safely access fn attribute
                fn = getattr(tool_def, "fn", None)
                description = getattr(tool_def, "description", None)

                if fn:
                    # Ensure the function has a docstring (required by LangChain)
                    if not fn.__doc__ and description:
                        fn.__doc__ = description

                    # Check if function is async
                    is_async = inspect.iscoroutinefunction(fn)

                    # Convert to StructuredTool
                    if is_async:
                        # For async functions, pass coroutine parameter
                        tool = StructuredTool.from_function(
                            fn,
                            name=tool_name,
                            description=description,
                            coroutine=fn,
                        )
                    else:
                        # For sync functions, standard conversion
                        tool = StructuredTool.from_function(
                            fn,
                            name=tool_name,
                            description=description,
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
