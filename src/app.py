"""
Aplicação principal do servidor FastMCP para o Rio de Janeiro.
"""

from fastapi import Request
from fastapi.responses import PlainTextResponse
from typing import Optional, List
from loguru import logger

from src.config.settings import Settings
from src.middleware.check_token import CheckTokenMiddleware
from src.tools.calculator import (
    add,
    subtract,
    multiply,
    divide,
    power,
)
from src.tools.datetime_tools import get_current_time, format_greeting

from src.tools.equipments_tools import (
    get_equipments_categories,
    get_equipments,
    get_equipments_instructions,
)

from src.tools.search import get_google_search

from src.resources import get_districts_list, get_rio_basic_info, get_greeting_message

from src.config.env import IS_LOCAL

if IS_LOCAL:
    from mcp.server.fastmcp import FastMCP
else:
    from fastmcp import FastMCP


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
    )

    if not IS_LOCAL:
        mcp.add_middleware(CheckTokenMiddleware())

        @mcp.custom_route("/health", methods=["GET"])
        async def health_check(request: Request) -> PlainTextResponse:
            return PlainTextResponse("OK")

    # Configuração de logging
    logger.info(f"Inicializando {Settings.SERVER_NAME} v{Settings.VERSION}")

    # ===== REGISTRAR TOOLS =====

    # Tools de calculadora
    @mcp.tool()
    def calculator_add(a: float, b: float) -> float:
        """Soma dois números"""
        return add(a, b)

    @mcp.tool()
    def calculator_subtract(a: float, b: float) -> float:
        """Subtrai dois números"""
        return subtract(a, b)

    @mcp.tool()
    def calculator_multiply(a: float, b: float) -> float:
        """Multiplica dois números"""
        return multiply(a, b)

    @mcp.tool()
    def calculator_divide(a: float, b: float) -> float:
        """Divide dois números"""
        return divide(a, b)

    @mcp.tool()
    def calculator_power(base: float, exponent: float) -> float:
        """Calcula a potência de um número"""
        return power(base, exponent)

    # Tools de data/hora
    @mcp.tool()
    def time_current() -> str:
        """Obtém a hora atual no Rio de Janeiro"""
        return get_current_time()

    @mcp.tool()
    def greeting_format() -> str:
        """Gera uma saudação personalizada baseada no horário"""
        return format_greeting()

    @mcp.tool()
    async def google_search(query: str) -> str:
        """Obtém os resultados da busca no Google"""
        response = await get_google_search(query)
        import json

        print(50 * "=")
        print(json.dumps(response, indent=4, ensure_ascii=False))
        return response

    @mcp.tool()
    async def equipments_by_address(
        address: str, categories: Optional[List[str]] = []
    ) -> dict:
        """
        Obtém os equipamentos mais proximos de um endereço.
        Args:
            address: Endereço do equipamento
            categories: Lista de categorias de equipamentos a serem filtrados. Deve obrigatoriamente seguir o nome exato das categorias retornadas na tool `equipments_instructions` na secao `categorias`.
        Returns:
            Lista de equipamentos
        """
        response = await get_equipments(address=address, categories=categories)
        return {
            "instructions": "Retorne todos os equipamentos referente a busca do usuario, acompanhado de todas as informacoes disponiveis sobre o equipamento",
            "equipamentos": response,
        }

    @mcp.tool()
    async def equipments_instructions() -> dict:
        """
        Utilizar sempre que o usuario entrar em alguma conversa tematica e seja necessario o redirecionamento para algum equipamento publico
        """
        instructions = await get_equipments_instructions()
        categories = await get_equipments_categories()
        return {
            "next_too_instructions": "**Atenção:** Para localizar os equipamentos mais próximos, *você deve obrigatoriamente solicitar o endereço do usuário*. Após o usuário fornecer o endereço, *você deve imediatamente chamar a tool `equipments_by_address`* utilizando o endereço informado. **Não se esqueça de chamar a tool `equipments_by_address` após o endereço ser informado.** A ferramenta `equipments_by_address` exige o parametro `categories` esse deve seguir o nome exato das categorias disponiveis na secao `categorias` NAO UTILLIZE CATEGORIAS DAS INSTRUCOES!!!. NÃO É NECESSARIO CHAMAR A TOOL `google_search` para buscar informacoes sobre os equipamentos ou endereço, pois a tool `equipments_by_address` já retorna todas as informacoes necessárias.",
            "instrucoes": instructions,
            "categorias": categories,
        }

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
