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
from src.utils.log import logger

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
        logger.info("Conexão com o servidor MCP encerrada.")

    async def connect(self):
        """Establishes connection to MCP server"""
        self._client = stdio_client(self.server_params)
        self.read, self.write = await self._client.__aenter__()
        session = ClientSession(self.read, self.write)
        self.session = await session.__aenter__()
        logger.info(f"Connected to MCP server: {self.server_params}")
        await self.session.initialize()

    async def get_available_tools(self) -> List[Any]:
        """Retrieve a list of available tools from the MCP server."""
        if not self.session:
            raise RuntimeError("Not connected to MCP server")
        return await self.session.list_tools()

    async def call_tool(self, tool_name: str, **kwargs) -> Any:
        """Execute a tool with the given arguments."""
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
        # --- ALTERAÇÃO AQUI ---
        # Adicionado um atributo para armazenar o histórico da conversa.
        self.history: List[Content] = []

    def get_client(self):
        """Retorna a instância do cliente Gemini."""
        return self.client

    async def get_tools_declarations(self, mcp_client):
        """Get tool declarations for Gemini without creating callables"""
        mcp_tools = await mcp_client.get_available_tools()
        system_prompt_tools = ""
        tool_declarations = []
        for tool in mcp_tools.tools:
            parsed_parameters = json.loads(
                json.dumps(tool.inputSchema)
                .replace("object", "OBJECT")
                .replace("string", "STRING")
                .replace("number", "NUMBER")
                .replace("boolean", "BOOLEAN")
                .replace("array", "ARRAY")
                .replace("integer", "INTEGER")
            )
            system_prompt_tools += f"Tool Name: {tool.name}: {tool.description}\n"
            system_prompt_tools += f"Parameters: {json.dumps(parsed_parameters, indent=4, ensure_ascii=False)}\n"
            declaration = FunctionDeclaration(
                name=tool.name,
                description=tool.description,
                parameters=parsed_parameters,
            )
            tool_declarations.append(declaration)
        return [Tool(function_declarations=tool_declarations)], system_prompt_tools

    # --- ALTERAÇÃO AQUI ---
    # A função original 'generate_content' foi transformada em 'start_chat_session'
    # para gerenciar o loop da conversa. A lógica interna foi preservada.
    async def start_chat_session(
        self,
        system_prompt: str,
        model: str = "gemini-2.5-flash",
        temperature: float = 0.0,
    ):
        """Inicia e gerencia uma sessão de chat interativa e contínua."""
        print("Bem-vindo ao chat! Digite 'sair' para terminar.")
        print("-" * 30)

        # O 'async with' agora envolve todo o loop do chat para manter a conexão.
        async with self.mcp_manager as mcp_client:
            # A preparação das ferramentas e configuração acontece uma vez, no início.
            tools, system_prompt_tools = await self.get_tools_declarations(
                mcp_client=mcp_client
            )

            full_system_prompt = system_prompt + "\n" + system_prompt_tools

            generation_config = GenerateContentConfig(
                temperature=temperature,
                thinking_config=ThinkingConfig(thinking_budget=-1),
                tools=tools,
                response_mime_type="text/plain",
                system_instruction=[Part.from_text(text=full_system_prompt)],
            )

            # O cliente é obtido uma vez, respeitando a estrutura original.
            client = self.get_client()

            # Loop principal da conversa.
            while True:
                try:
                    query = input("Você: ")
                    if query.lower() in ["sair", "exit", "quit"]:
                        print("Até logo!")
                        break

                    # Adiciona a mensagem do usuário ao histórico da classe.
                    self.history.append(Content(role="user", parts=[Part(text=query)]))

                    print("Gemini está pensando...")

                    # --- LÓGICA ORIGINAL PRESERVADA ---
                    # Este é o loop de chamada de ferramenta do seu código original,
                    # agora operando sobre `self.history`.
                    while True:
                        response = await client.aio.models.generate_content(
                            model=model,
                            contents=self.history,  # Usa o histórico da classe
                            config=generation_config,
                        )
                        candidate = response.candidates[0]

                        # Adiciona a resposta do modelo (seja texto ou chamada de função) ao histórico.
                        self.history.append(candidate.content)

                        if (
                            not candidate.content.parts
                            or not candidate.content.parts[0].function_call
                        ):
                            # Se não houver chamada de função, o turno terminou.
                            # Imprime a resposta e sai do loop de ferramentas.
                            final_text = candidate.content.parts[0].text
                            print(f"Gemini: {final_text}")
                            break

                        # O modelo fez uma chamada de função.
                        function_call = candidate.content.parts[0].function_call
                        tool_name = function_call.name
                        tool_args = dict(function_call.args)
                        logger.info(
                            f"Modelo solicitou chamada da ferramenta: {tool_name} com args: {tool_args}"
                        )

                        try:
                            tool_result = await mcp_client.call_tool(
                                tool_name, **tool_args
                            )
                            # logger.info(
                            #     f"Tool result: {json.dumps(tool_result, indent=4, ensure_ascii=False)}"
                            # )
                        except Exception as e:
                            logger.error(f"Error executing tool '{tool_name}': {e}")
                            tool_result = f"Error: {str(e)}"

                        # Adiciona o resultado da ferramenta ao histórico para a próxima iteração do modelo.
                        # CORREÇÃO CRÍTICA: A role para a resposta da ferramenta deve ser 'tool'.
                        self.history.append(
                            Content(
                                role="tool",
                                parts=[
                                    Part.from_function_response(
                                        name=tool_name,
                                        response={"result": tool_result},
                                    )
                                ],
                            )
                        )
                        # O loop de ferramentas continua, enviando o resultado de volta ao modelo.

                    print("-" * 30)

                except KeyboardInterrupt:
                    print("\nAté logo!")
                    break
                except Exception as e:
                    logger.error(f"Ocorreu um erro inesperado: {e}", exc_info=True)
                    break


async def main():
    mcp_server_params = StdioServerParameters(
        command="uv", args=["run", "python", "src/main.py"]
    )

    gemini = GeminiService(mcp_server_params=mcp_server_params)

    await gemini.start_chat_session(
        system_prompt=SYSTEM_PROMPT_EAI,
        model="gemini-2.5-flash",  # Recomendo modelos mais recentes
    )


if __name__ == "__main__":
    asyncio.run(main())
