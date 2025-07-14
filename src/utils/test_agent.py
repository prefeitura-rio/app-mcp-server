import asyncio
from typing import Any, List, Dict
import json

from google import genai
from google.genai.types import (
    Tool,
    UrlContext,
    ThinkingConfig,
    GenerateContentConfig,
    GoogleSearch,
    Content,
    Part,
    FunctionDeclaration,
)
from src.config import env
import logging

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from src.utils.prompts import SYSTEM_PROMPT_EAI


class MCPToolsManager:
    def __init__(self, server_params: StdioServerParameters):
        """Initialize the MCP client with server parameters"""
        self.server_params = server_params
        self.session = None
        self._client = None

    async def __aenter__(self):
        """Async context manager entry"""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        if self.session:
            await self.session.__aexit__(exc_type, exc_val, exc_tb)
        if self._client:
            await self._client.__aexit__(exc_type, exc_val, exc_tb)

    async def connect(self):
        """Establishes connection to MCP server"""
        self._client = stdio_client(self.server_params)
        self.read, self.write = await self._client.__aenter__()
        session = ClientSession(self.read, self.write)
        self.session = await session.__aenter__()
        logger.info(f"Connected to MCP server: {self.server_params}")
        await self.session.initialize()

    async def get_available_tools(self) -> List[Any]:
        """
        Retrieve a list of available tools from the MCP server.
        """
        if not self.session:
            raise RuntimeError("Not connected to MCP server")

        return await self.session.list_tools()

    async def call_tool(self, tool_name: str, **kwargs) -> Any:
        """
        Execute a tool with the given arguments.

        Args:
            tool_name: The name of the tool to execute
            **kwargs: Arguments to pass to the tool

        Returns:
            The result of the tool execution
        """
        if not self.session:
            raise RuntimeError("Not connected to MCP server")

        response = await self.session.call_tool(tool_name, arguments=kwargs)
        return response.content[0].text


class GeminiService:
    def __init__(self, mcp_server_params: StdioServerParameters):
        """Inicializa o cliente Gemini com as configurações do ambiente."""
        self.api_key = env.GEMINI_API_KEY
        self.client = genai.Client(api_key=self.api_key)
        self.mcp_manager = MCPToolsManager(server_params=mcp_server_params)

    def get_client(self):
        """Retorna a instância do cliente Gemini."""
        return self.client

    async def get_tools_declarations(self, mcp_client):
        """Get tool declarations for Gemini without creating callables"""
        # Get available tools from MCP server
        mcp_tools = await mcp_client.get_available_tools()

        # Prepare system prompt and tool declarations
        system_prompt_tools = ""
        tool_declarations = []

        for tool in mcp_tools.tools:
            # Parse parameters for Gemini format
            parsed_parameters = json.loads(
                json.dumps(tool.inputSchema)
                .replace("object", "OBJECT")
                .replace("string", "STRING")
                .replace("number", "NUMBER")
                .replace("boolean", "BOOLEAN")
                .replace("array", "ARRAY")
                .replace("integer", "INTEGER")
            )

            # Build system prompt
            system_prompt_tools += f"Tool Name: {tool.name}: {tool.description}\n"
            system_prompt_tools += f"Parameters: {json.dumps(parsed_parameters, indent=4, ensure_ascii=False)}\n"

            # Create function declaration for Gemini
            declaration = FunctionDeclaration(
                name=tool.name,
                description=tool.description,
                parameters=parsed_parameters,
            )
            tool_declarations.append(declaration)

        return [Tool(function_declarations=tool_declarations)], system_prompt_tools

    async def generate_content(
        self,
        query: str,
        system_prompt: str = None,
        model: str = "gemini-2.5-flash",
        temperature: float = 0.0,
        retry_attempts: int = 3,
    ):
        """
        Generate content using Gemini with MCP tools support.

        Args:
            query: The user query
            model: The Gemini model to use
            temperature: Temperature for generation
            retry_attempts: Number of retry attempts
        """

        # Use the MCP manager as a context manager for the entire generation process
        async with self.mcp_manager as mcp_client:
            # Get tool declarations
            tools, system_prompt_tools = await self.get_tools_declarations(
                mcp_client=mcp_client
            )

            logger.info(f"Iniciando pesquisa Google para: {query}")
            logger.info("Gerando conteúdo com Gemini...")

            contents = [Content(role="user", parts=[Part(text=query)])]

            generation_config = GenerateContentConfig(
                temperature=temperature,
                thinking_config=ThinkingConfig(
                    thinking_budget=-1,
                ),
                tools=tools,
                response_mime_type="text/plain",
                system_instruction=(
                    [
                        Part.from_text(text=system_prompt + "\n" + system_prompt_tools),
                    ]
                    if system_prompt
                    else None
                ),
            )
            client = self.get_client()
            while True:

                response = await client.aio.models.generate_content(
                    model=model,
                    contents=contents,
                    config=generation_config,
                )
                candidate = response.candidates[0]

                # Check if the model decided to call a function
                if (
                    not candidate.content.parts
                    or not candidate.content.parts[0].function_call
                ):
                    # If no function call, we are done. Return the final content.
                    contents.append(candidate.content)
                    return contents

                # The model made a function call
                function_call = candidate.content.parts[0].function_call
                tool_name = function_call.name
                tool_args = dict(function_call.args)
                logger.info(
                    f"Modelo solicitou chamada da ferramenta: {tool_name} com args: {tool_args}"
                )

                # Add the model's request to the conversation history
                contents.append(candidate.content)

                # Execute the tool using the connected MCP client
                try:
                    tool_result = await mcp_client.call_tool(tool_name, **tool_args)
                    # logger.info(f"Resultado da ferramenta '{tool_name}': {tool_result}")
                except Exception as e:
                    logger.error(f"Error executing tool '{tool_name}': {e}")
                    tool_result = f"Error: {str(e)}"

                # Add the tool's response to the conversation history for the next turn
                contents.append(
                    Content(
                        role="user",
                        parts=[
                            Part.from_function_response(
                                name=tool_name,
                                response={"result": tool_result},
                            )
                        ],
                    )
                )


async def main():
    mcp_server_params = StdioServerParameters(
        command="uv", args=["run", "python", "src/main.py"]
    )

    gemini = GeminiService(mcp_server_params=mcp_server_params)

    # query = f"""
    #     Minha esposa esta em trabalho de parto! o que eu faço?
    #     meu endereco é Avenida Presidente Vargas, 1
    # """
    query = f"""
        po, conheço uma pessoa que recebe bolsa familia mas nem precisa, tem carro, casa boa... como que eu faço pra denunciar isso? é sacanagem com quem precisa de vdd
    """
    # Generate content with tools
    content = await gemini.generate_content(
        query=query,
        system_prompt=SYSTEM_PROMPT_EAI,
        model="gemini-2.5-flash",
    )

    print("Final response:")
    for c in content:
        if c.role == "model" and c.parts:
            for part in c.parts:
                if hasattr(part, "text") and part.text:
                    print(part.text)


if __name__ == "__main__":
    asyncio.run(main())
