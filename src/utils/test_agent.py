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

    def call_tool(self, tool_name: str) -> Any:
        """
        Create a callable function for a specific tool.
        This allows us to execute database operations through the MCP server.

        Args:
            tool_name: The name of the tool to create a callable for

        Returns:
            A callable async function that executes the specified tool
        """
        if not self.session:
            raise RuntimeError("Not connected to MCP server")

        async def callable(*args, **kwargs):
            response = await self.session.call_tool(tool_name, arguments=kwargs)
            return response.content[0].text

        return callable
            
            
class GeminiService:
    def __init__(self):
        """Inicializa o cliente Gemini com as configurações do ambiente."""
        self.api_key = env.GEMINI_API_KEY
        self.client = genai.Client(api_key=self.api_key)
        
    def get_client(self):
        """Retorna a instância do cliente Gemini."""
        return self.client
    
    async def generate_content(
        self,
        query: str,
        model: str = "gemini-2.5-flash",
        temperature: float = 0.0,
        retry_attempts: int = 3,
        tools: List[Tool] = None,
        tool_callables: Dict[str, Any] = None
    ):
        """
        Generate content using Gemini with MCP tools support.
        
        Args:
            query: The user query
            model: The Gemini model to use
            temperature: Temperature for generation
            retry_attempts: Number of retry attempts
            tools: List of Tool objects for Gemini
            tool_callables: Dictionary mapping tool names to callable functions
        """
        if tools is None:
            tools = []
        if tool_callables is None:
            tool_callables = {}
            
        logger.info(f"Iniciando pesquisa Google para: {query}")
        
        logger.info("Gerando conteúdo com Gemini...")
        contents = [
            Content(role="user", parts=[Part(text=query)])
        ]
        
        generation_config = GenerateContentConfig(
            temperature=temperature,
            thinking_config=ThinkingConfig(
                thinking_budget=-1,
            ),
            tools=tools,
            response_mime_type="text/plain",
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
            if not candidate.content.parts or not candidate.content.parts[0].function_call:
                # If no function call, we are done. Return the final content.
                contents.append(candidate.content)
                return contents
            
            # The model made a function call
            function_call = candidate.content.parts[0].function_call
            tool_name = function_call.name
            tool_args = dict(function_call.args)
            logger.info(f"Modelo solicitou chamada da ferramenta: {tool_name} com args: {tool_args}")
            
            # Add the model's request to the conversation history
            contents.append(candidate.content)
            
            # Check if we have a callable for this tool
            if tool_name not in tool_callables:
                raise ValueError(f"Tool '{tool_name}' not found in available callables")
            
            # Execute the tool
            tool_result = await tool_callables[tool_name](**tool_args)
            
            logger.info(f"Resultado da ferramenta '{tool_name}': {tool_result}")

            # Add the tool's response to the conversation history for the next turn
            contents.append(Content(
                role="user",
                parts=[Part.from_function_response(
                    name=tool_name,
                    response={"result": tool_result},
                )]
            ))
    
    
async def main():
    server_params = StdioServerParameters(
        command="uv",
        args=["run", "python", "src/main.py"]
    )
    
    async with MCPToolsManager(server_params) as mcp_client:
        # Get available tools from MCP server
        mcp_tools = await mcp_client.get_available_tools()
        
        # Prepare system prompt and tool declarations
        system_prompt_tools = ""
        tool_declarations = []
        tool_callables = {}  # Dictionary to map tool names to callable functions
        
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
            
            # Create callable function and add to mapping
            tool_callables[tool.name] = mcp_client.call_tool(tool.name)
    
        # Initialize Gemini service
        gemini = GeminiService()

        # Create query
        query = f"""
        Quanto é ((73ˆ5)*10) + 93
        """
        
        # Create tools list for Gemini
        tools = [Tool(function_declarations=tool_declarations)]
        
        # Generate content with tools
        content = await gemini.generate_content(
            query=query, 
            tools=tools,
            tool_callables=tool_callables
        )
        
        print("Final response:")
        for c in content:
            if c.role == "model" and c.parts:
                for part in c.parts:
                    if hasattr(part, 'text') and part.text:
                        print(part.text)
    

if __name__ == "__main__":
    asyncio.run(main())