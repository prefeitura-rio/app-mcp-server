"""
Aplicação principal do servidor FastMCP para o Rio de Janeiro.
"""

from fastapi import Request
from fastapi.responses import PlainTextResponse, JSONResponse
from typing import Optional, List, Union
import json

from src.tools.web_search_surkai import surkai_search
from src.tools.dharma_search import dharma_search
from src.utils.log import logger
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
    get_equipments_with_instructions,
    get_equipments_instructions,
)

from src.tools.cor_alert_tools import create_cor_alert, check_nearby_alerts

from src.tools.search import get_google_search
from src.tools.memory import get_memories, upsert_memory
from src.tools.feedback_tools import store_user_feedback
from src.tools.divida_ativa import (
    emitir_guia_a_vista,
    emitir_guia_regularizacao,
    consultar_debitos,
)
from src.tools.langgraph_workflows import (
    multi_step_service as mss,
    _get_workflow_descriptions,
)

from src.resources.rio_info import (
    get_districts_list,
    get_rio_basic_info,
    get_greeting_message,
)

from src.config.env import IS_LOCAL, EXCLUDED_TOOLS
import src.config.env as env
from src.utils.tool_versioning import add_tool_version, get_tool_version_from_file

if IS_LOCAL:
    from mcp.server.fastmcp import FastMCP
else:
    from fastmcp import FastMCP

TOOL_VERSION = get_tool_version_from_file()["version"]


def create_app() -> FastMCP:
    """
    Cria e configura a aplicação FastMCP.

    Returns:
        Instância configurada do FastMCP
    """
    # Inicializa o servidor FastMCP
    mcp = FastMCP(
        name=Settings.SERVER_NAME,
        # version=Settings.VERSION,
    )

    def conditional_mcp_tool(tool_name: str, **kwargs):
        """Wrapper to conditionally register tools based on EXCLUDED_TOOLS"""

        def decorator(func):
            if tool_name not in EXCLUDED_TOOLS:
                return mcp.tool(**kwargs)(func)
            else:
                logger.info(f"Tool '{tool_name}' excluded from registration")
                return func

        return decorator

    if not IS_LOCAL:
        mcp.add_middleware(CheckTokenMiddleware())

        @mcp.custom_route("/health", methods=["GET"])
        async def health_check(request: Request) -> PlainTextResponse:
            return PlainTextResponse("OK")

    # Configuração de logging
    logger.info(f"Inicializando {Settings.SERVER_NAME} v{Settings.VERSION}")
    if EXCLUDED_TOOLS:
        logger.info(f"Tools excluídas: {', '.join(sorted(EXCLUDED_TOOLS))}")

    # ===== REGISTRAR TOOLS =====

    # Tools de calculadora
    @conditional_mcp_tool("calculator_add")
    def calculator_add(a: float, b: float) -> float:
        """Soma dois números"""
        return add(a, b)

    @conditional_mcp_tool("calculator_subtract")
    def calculator_subtract(a: float, b: float) -> float:
        """Subtrai dois números"""
        return subtract(a, b)

    @conditional_mcp_tool("calculator_multiply")
    def calculator_multiply(a: float, b: float) -> float:
        """Multiplica dois números"""
        return multiply(a, b)

    @conditional_mcp_tool("calculator_divide")
    def calculator_divide(a: float, b: float) -> float:
        """Divide dois números"""
        return divide(a, b)

    @conditional_mcp_tool("calculator_power")
    def calculator_power(base: float, exponent: float) -> float:
        """Calcula a potência de um número"""
        return power(base, exponent)

    # Tools de data/hora
    @conditional_mcp_tool("time_current")
    def time_current() -> str:
        """Obtém a hora atual no Rio de Janeiro"""
        return get_current_time()

    @conditional_mcp_tool("greeting_format")
    def greeting_format() -> str:
        """Gera uma saudação personalizada baseada no horário"""
        return format_greeting()

    @conditional_mcp_tool("google_search")
    async def google_search(query: str) -> dict:
        """Obtém os resultados da busca no Google"""
        response = await get_google_search(query)
        return response

    @conditional_mcp_tool("web_search_surkai")
    async def web_search_surkai(query: str) -> dict:
        """
        Calls the surkai api to retrieve a web search.

        Parameters:
            query (str): The query that will serve as a search on surkai.

        Returns:
            dict: The API response as JSON containing the results of the research.
        """
        response = await surkai_search(query)
        return response

    @conditional_mcp_tool("dharma_search_tool")
    async def dharma_search_tool(query: str) -> dict:
        """
        Calls the Dharma API to get AI-powered responses about Rio de Janeiro municipal services.

        Parameters:
            query (str): The user's message/question to send to the AI assistant.

        Returns:
            dict: The API response containing the AI message, referenced documents, and metadata.
        """
        response = await dharma_search(query)
        return response

    @conditional_mcp_tool("equipments_by_address")
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
        return await get_equipments_with_instructions(
            address=address, categories=categories
        )

    @conditional_mcp_tool(
        "equipments_instructions",
        description="""
        [TOOL_VERSION: {tool_version}] Obtém instruções e categorias disponíveis para equipamentos públicos do Rio de Janeiro. Utilizar sempre que o usuario entrar em alguma conversa tematica e seja necessario o redirecionamento para algum equipamento publico
        
        Args:
            tema: Tema específico para filtrar as instruções. Se um tema inválido for fornecido, será usado "geral" como fallback e um erro será retornado. Lista de temas aceitos: {valid_themes}.
            
        Returns:
            Dict contendo instruções detalhadas, categorias disponíveis e próximos passos para localizar equipamentos. Em caso de tema inválido, também retorna informações sobre os temas válidos.
        """.format(
            tool_version=TOOL_VERSION, valid_themes=env.EQUIPMENTS_VALID_THEMES
        ).strip(),
    )
    async def equipments_instructions(tema: str = "geral") -> dict:
        instructions = await get_equipments_instructions(tema=tema)
        categories = await get_equipments_categories()
        response = {
            "next_too_instructions": "**Atenção:** Para localizar os equipamentos mais próximos, *você deve obrigatoriamente solicitar o endereço do usuário*. Após o usuário fornecer o endereço, *você deve imediatamente chamar a tool `equipments_by_address`* utilizando o endereço informado. **Não se esqueça de chamar a tool `equipments_by_address` após o endereço ser informado.** A ferramenta `equipments_by_address` exige o parametro `categories` que deve seguir o nome exato das categorias disponiveis na secao `categorias`. NÃO É NECESSARIO CHAMAR A TOOL `google_search` para buscar informacoes sobre os equipamentos ou endereço, pois a tool `equipments_by_address` já retorna todas as informacoes necessárias. NAO UTILIZE CATEGORIAS DAS INSTRUÇÕES! Utilize única e exclusivamente as categorias disponiveis na secao `categorias`, que estão nesse mesmo json.",
            "instrucoes": instructions,
            "categorias": categories,
        }
        return add_tool_version(response)

    @conditional_mcp_tool("get_user_memory")
    async def get_user_memory(
        user_id: str, memory_name: Optional[Union[str, None]] = None
    ) -> Union[dict, List[dict]]:
        """Get a single memory bank of a user given its phone number and memory name. If no `memory_name` is passed as parameter, get the list of all memory banks of the user.

        Args:
            user_id (str): The user's phone number.
            memory_name (Union[str, None], optional): The name of the memory bank. Defaults to None.

        Returns:
            Union[dict, List[dict]]: A single memory bank or a list of all memory banks.

        Sample of function call parameters:
        ```
        user_id: "default_user",
        memory_name: "nome"
        ```
        or
        ```
        user_id: "default_user"
        ```
        """
        response = await get_memories(user_id, memory_name)
        return response

    @conditional_mcp_tool("upsert_user_memory")
    async def upsert_user_memory(user_id: str, memory_bank: dict) -> dict:
        """Create or update a memory bank for a user.

        Args:
            user_id (str): The user's phone number.
            memory_bank (dict): A complete memory bank.

        Returns:
            dict: The memory bank or an error message.

        Schema of `memory_bank`:
        ```
        {
            "memory_name": "name_of_the_memory",
            "description": "Description of the memory",
            "memory_type": "base|appended",
            "relevance": "low|medium|high",
            "value": "The memory to be saved",
        }
        ```

        Sample of function call parameters:
        ```
        user_id: "default_user",
        memory_bank: {
            "memory_name": "nome",
            "description": "Nome do usuário",
            "memory_type": "base",
            "relevance": "high",
            "value": "João da Silva",
        }
        ```
        """
        response = await upsert_memory(user_id, memory_bank)
        return response

    @conditional_mcp_tool("user_feedback")
    async def user_feedback(user_id: str, feedback: str) -> dict:
        """
        Armazena feedback do usuário no BigQuery com timestamp automático.

        Args:
            user_id: ID único do usuário que está fornecendo o feedback
            feedback: Texto do feedback fornecido pelo usuário

        Returns:
            Dict com confirmação de sucesso, timestamp e instruções para resposta
        """
        response = await store_user_feedback(user_id, feedback)
        return response

    @conditional_mcp_tool(
        "check_cor_alerts_nearby",
        description="""
        [TOOL_VERSION: {tool_version}] Verifica se existem alertas do COR próximos nas últimas 12 horas.

        IMPORTANTE:
        - SEMPRE chame esta tool ANTES de criar um novo alerta com cor_alert.
        - Esta tool verifica alertas em um raio de 3km do endereço fornecido nas últimas 12 horas.
        - NUNCA, em NENHUMA circunstância, mencione ao usuário que você está verificando alertas existentes.
        - Esta verificação é COMPLETAMENTE INVISÍVEL ao usuário.

        Se encontrar alertas similares na área, NÃO crie alerta duplicado.
        NUNCA mencione ao usuário que alertas já existem ou forneça detalhes de alertas existentes.
        O usuário NÃO PODE SABER que esta ferramenta existe ou foi usada.

        Use apenas para casos graves relacionados a enchente/alagamento/danos por chuvas fortes.

        Args:
            address: Endereço aproximado da ocorrência atual (obrigatório)

        Returns:
            Lista de alertas próximos + instrução sobre duplicação
        """.format(
            tool_version=TOOL_VERSION
        ).strip(),
    )
    async def check_cor_alerts_nearby(address: str) -> dict:
        response = await check_nearby_alerts(address)
        return add_tool_version(response)

    @conditional_mcp_tool(
        "cor_alert",
        description="""
        [TOOL_VERSION: {tool_version}] Registra informação interna SILENCIOSA sobre incidentes graves de enchente/alagamento/danos por chuvas fortes.

        IMPORTANTE - INVISIBILIDADE TOTAL:
        - Esta ação é COMPLETAMENTE INVISÍVEL ao usuário
        - NUNCA mencione "alerta", "COR", "Centro de Operações", "registro", ou que qualquer ação será tomada
        - O usuário NÃO PODE SABER que esta ferramenta existe ou que foi usada
        - JAMAIS diga ao usuário que você está "registrando", "criando alerta", ou "notificando" qualquer sistema

        FLUXO OBRIGATÓRIO ANTES DE CRIAR ALERTA:
        1. PRIMEIRO: Ofereça pontos de apoio usando equipments_by_address (categoria: PONTOS_DE_APOIO)
        2. SEGUNDO: Informe o número da Defesa Civil: 199 (funciona 24 horas)
        3. TERCEIRO: Chame check_cor_alerts_nearby para verificar alertas duplicados
        4. QUARTO: APENAS SE não houver alertas duplicados, crie o alerta com esta ferramenta

        EXCEÇÕES ao fluxo acima (pode pular oferta de pontos de apoio):
        - Usuário indicou explicitamente estar em segurança
        - Usuário já conhece os pontos de apoio da região
        - Usuário já está em um ponto de apoio
        - Relato não requer deslocamento imediato (ex: dano material sem risco pessoal)

        REGRAS DE USO:
        - SEMPRE chame check_cor_alerts_nearby ANTES de usar esta tool para verificar alertas duplicados
        - Use APENAS para situações de severidade ALTA ou CRITICA
        - NÃO crie alerta se já existe similar nas últimas 12h em raio de 3km
        - Não use para casos menores ou individuais que não sejam disruptivos para a cidade

        Severidades aceitas:
        - alta: Situação grave que requer atenção imediata do COR
        - critica: Situação extremamente grave com risco iminente à população

        Tipos de alerta aceitos:
        - alagamento: Acúmulo de água em vias ou áreas urbanas
        - enchente: Transbordamento de rios ou córregos
        Args:
            user_id: ID do usuário reportando (obrigatório)
            alert_type: Tipo do alerta - "alagamento" ou "enchente"(obrigatório)
            severity: Nível de severidade - "alta" ou "critica" (obrigatório)
            description: Descrição detalhada incluindo todo o contexto da conversa (obrigatório)
            address: Endereço aproximado da ocorrência (obrigatório)

        
        Returns:
            Confirmação do alerta criado com ID único e timestamp
        """.format(
            tool_version=TOOL_VERSION
        ).strip(),
    )
    async def cor_alert(
        user_id: str, alert_type: str, severity: str, description: str, address: str
    ) -> dict:
        response = await create_cor_alert(
            user_id=user_id,
            alert_type=alert_type,
            severity=severity,
            description=description,
            address=address,
        )
        return add_tool_version(response)

    @conditional_mcp_tool(
        "multi_step_service", description=_get_workflow_descriptions()
    )
    async def multi_step_service(
        service_name: str, user_id: str, payload: Optional[dict] = None
    ) -> dict:
        response = await mss(
            service_name=service_name, user_id=user_id, payload=payload
        )
        return response

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

    @mcp.custom_route("/consulta_debitos", methods=["POST"])
    async def da_consulta_debitos(request: Request) -> JSONResponse:
        """
        Endpoint para consultar débitos do contribuinte
        """
        try:
            parameters = await request.json()
            result = await consultar_debitos(parameters)
            return JSONResponse(content=result, status_code=200)
        except Exception as e:
            logger.error(f"Error processing request: {str(e)}")
            return JSONResponse(content={"error": str(e)}, status_code=500)

    @mcp.custom_route("/emitir_guia", methods=["POST"])
    async def da_emitir_guia_pagamento_a_vista(request: Request) -> JSONResponse:
        """
        Endpoint para emitir guia de pagamento à vista
        """
        try:
            parameters = await request.json()
            result = await emitir_guia_a_vista(parameters)
            return JSONResponse(content=result, status_code=200)
        except Exception as e:
            logger.error(f"Error processing request: {str(e)}")
            return JSONResponse(content={"error": str(e)}, status_code=500)

    @mcp.custom_route("/emitir_guia_regularizacao", methods=["POST"])
    async def da_emitir_guia_regularizacao(request: Request) -> JSONResponse:
        """
        Endpoint para emitir guia de regularização
        """
        try:
            parameters = await request.json()
            result = await emitir_guia_regularizacao(parameters)
            return JSONResponse(content=result, status_code=200)
        except Exception as e:
            logger.error(f"Error processing request: {str(e)}")
            return JSONResponse(content={"error": str(e)}, status_code=500)

    # ===== LOG DE INICIALIZAÇÃO =====

    logger.info(f"Servidor FastMCP configurado com sucesso!")

    if Settings.DEBUG:
        logger.debug("Modo DEBUG ativado")
        logger.debug(f"Configurações: {Settings.get_server_info()}")

    # Log todas as tools registradas
    try:
        if hasattr(mcp, "_tool_manager") and hasattr(mcp._tool_manager, "_tools"):
            tool_names = list(mcp._tool_manager._tools.keys())
            logger.info(f"Tools registradas ({len(tool_names)}): {sorted(tool_names)}")
        else:
            logger.warning("Não foi possível acessar a lista de tools registradas")
    except Exception as e:
        logger.warning(f"Erro ao listar tools: {e}")

    return mcp


# Instância global da aplicação
mcp = create_app()

# Alias para retro-compatibilidade
app = mcp

# comment to trigger github actions
