"""
Aplicação principal do servidor FastMCP para o Rio de Janeiro.
"""
from fastmcp import FastMCP
from loguru import logger

from src.middlewares.token_validation import create_bearer_middleware
from src.config import env

from .config import Settings
from .tools import (
    add, subtract, multiply, divide, power,
    get_current_time, format_greeting
)
from .resources import (
    get_districts_list, get_rio_basic_info, get_greeting_message
)


def create_app() -> FastMCP:
    """
    Cria e configura a aplicação FastMCP.
    
    Returns:
        Instância configurada do FastMCP
    """
    # Inicializa o servidor FastMCP
    mcp = FastMCP(
        name=Settings.SERVER_NAME,
        version=Settings.VERSION,
        middleware=[create_bearer_middleware(valid_tokens=env.VALID_TOKENS)]
    )
    
    # Configuração de logging
    logger.info(f"Inicializando {Settings.SERVER_NAME} v{Settings.VERSION}")
    
    # ===== REGISTRAR TOOLS =====
    
    # Tools de calculadora
    mcp.tool()(add)
    mcp.tool()(subtract) 
    mcp.tool()(multiply)
    mcp.tool()(divide)
    mcp.tool()(power)
    
    # Tools de data/hora
    mcp.tool()(get_current_time)
    mcp.tool()(format_greeting)
    
    # ===== REGISTRAR RESOURCES =====
    
    # Resource com lista de bairros
    mcp.resource(f"{Settings.RESOURCE_PREFIX}districts")(get_districts_list)
    
    # Resource com informações básicas do Rio
    mcp.resource(f"{Settings.RESOURCE_PREFIX}rio_info")(get_rio_basic_info)
    
    # Resource com mensagem de boas-vindas
    mcp.resource(f"{Settings.RESOURCE_PREFIX}greeting")(get_greeting_message)
    
    # ===== REGISTRAR PROMPTS =====
    
    @mcp.prompt("rio_assistant")
    def rio_assistant_prompt(context: str = "") -> str:
        """
        Prompt para assistente especializado em informações do Rio de Janeiro.
        
        Args:
            context: Contexto adicional para o prompt
            
        Returns:
            Prompt formatado para o assistente
        """
        base_prompt = """
        Você é um assistente especializado em informações sobre o Rio de Janeiro.
        
        Você tem acesso a:
        - Ferramentas de cálculo (soma, subtração, multiplicação, divisão, potência)
        - Informações atualizadas sobre data/hora no Rio de Janeiro
        - Lista de bairros do Rio de Janeiro
        - Informações básicas sobre a cidade
        - Saudações personalizadas baseadas no horário
        
        Sempre responda em português brasileiro e seja prestativo e cordial.
        Use as ferramentas disponíveis quando apropriado.
        """
        
        if context:
            base_prompt += f"\n\nContexto adicional: {context}"
            
        return base_prompt
    
    # ===== LOG DE INICIALIZAÇÃO =====
    
    logger.info(f"Servidor FastMCP configurado com sucesso!")
    logger.info(f"Tools registradas: calculadora (5), data/hora (2)")
    logger.info(f"Resources registrados: 3")
    logger.info(f"Prompts registrados: 1")
    
    if Settings.DEBUG:
        logger.debug("Modo DEBUG ativado")
        logger.debug(f"Configurações: {Settings.get_server_info()}")
    
    return mcp


# Instância global da aplicação
mcp = create_app()

# Alias para retro-compatibilidade (algumas partes ainda importam `app`)
app = mcp  # type: ignore

# Exporte um ASGI app para transporte HTTP (Streamable HTTP)
#   - Endpoint principal:   GET /mcp       (JSON-RPC)
#   - Endpoint de mensagens:POST /mcp/messages
http_app = mcp.http_app()


if __name__ == "__main__":
    # Para teste local
    import asyncio
    from fastmcp import Client
    
    async def test_server():
        """Função de teste simples para o servidor"""
        client = Client(app)
        
        async with client:
            # Testa uma ferramenta
            result = await client.call_tool("add", {"a": 5, "b": 3})
            print(f"Teste de soma: {result}")
            
            # Testa um resource
            districts = await client.get_resource("rio://resources/districts")
            print(f"Primeiros 3 bairros: {districts[:3]}")
            
            # Testa saudação
            greeting = await client.call_tool("format_greeting", {})
            print(f"Saudação: {greeting}")
    
    asyncio.run(test_server()) 