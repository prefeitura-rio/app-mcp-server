"""
Aplicação principal do servidor FastMCP para o Rio de Janeiro.
"""
from fastapi import Request
from fastapi.responses import PlainTextResponse
from fastmcp import FastMCP
from loguru import logger

from src.config.settings import Settings
from src.tools import (
    add, subtract, multiply, divide, power,
    get_current_time, format_greeting
)
from src.resources import (
    get_districts_list, get_rio_basic_info, get_greeting_message
)


def create_app() -> FastMCP:
    """
    Cria e configura a aplicação FastMCP.
    
    Returns:
        Instância configurada do FastMCP
    """
    # Inicializa o servidor FastMCP SEM middleware de autenticação para simplificar
    mcp = FastMCP(
        name=Settings.SERVER_NAME,
        version=Settings.VERSION,
    )
    
    @mcp.custom_route("/health", methods=["GET"])
    async def health_check(request: Request) -> PlainTextResponse:
        return PlainTextResponse("OK")
    
    # Configuração de logging
    logger.info(f"Inicializando {Settings.SERVER_NAME} v{Settings.VERSION}")
    
    # ===== REGISTRAR TOOLS =====
    
    # Tools de calculadora
    @mcp.tool
    def calculator_add(a: float, b: float) -> float:
        """Soma dois números"""
        return add(a, b)
    
    @mcp.tool
    def calculator_subtract(a: float, b: float) -> float:
        """Subtrai dois números"""
        return subtract(a, b)
    
    @mcp.tool
    def calculator_multiply(a: float, b: float) -> float:
        """Multiplica dois números"""
        return multiply(a, b)
    
    @mcp.tool
    def calculator_divide(a: float, b: float) -> float:
        """Divide dois números"""
        return divide(a, b)
    
    @mcp.tool
    def calculator_power(base: float, exponent: float) -> float:
        """Calcula a potência de um número"""
        return power(base, exponent)
    
    # Tools de data/hora
    @mcp.tool
    def time_current() -> str:
        """Obtém a hora atual no Rio de Janeiro"""
        return get_current_time()
    
    @mcp.tool
    def greeting_format() -> str:
        """Gera uma saudação personalizada baseada no horário"""
        return format_greeting()
    
    # ===== REGISTRAR RESOURCES =====
    
    # Resource com lista de bairros
    @mcp.resource(f"{Settings.RESOURCE_PREFIX}districts")
    def resource_districts():
        """Lista de bairros do Rio de Janeiro"""
        return get_districts_list()
    
    # Resource com informações básicas do Rio
    @mcp.resource(f"{Settings.RESOURCE_PREFIX}rio_info")
    def resource_rio_info():
        """Informações básicas sobre o Rio de Janeiro"""
        return get_rio_basic_info()
    
    # Resource com mensagem de boas-vindas
    @mcp.resource(f"{Settings.RESOURCE_PREFIX}greeting")
    def resource_greeting():
        """Mensagem de boas-vindas"""
        return get_greeting_message()
    
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

# Alias para retro-compatibilidade
app = mcp