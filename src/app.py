"""
Aplicação principal do servidor FastMCP para o Rio de Janeiro.
"""

# comment to trigger build

import os
import time

from fastapi import Request
from fastapi.responses import PlainTextResponse, JSONResponse
from typing import Optional, List, Union

from src.tools.web_search_surkai import surkai_search
from src.tools.dharma_search import dharma_search
from src.utils.log import logger

# Efeito de import: completa a cadeia TLS incompleta do SGRC (seta SSL_CERT_FILE/
# REQUESTS_CA_BUNDLE). Importado aqui, no topo, pra rodar ANTES de qualquer chamada
# ao SGRC (o ssl/aiohttp lê SSL_CERT_FILE no momento da conexão). É só efeito de
# import — sem statement no bloco (evita E402). Ver src/utils/sgrc_ca.py.
import src.utils.sgrc_ca  # noqa: F401

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

from src.tools.cor_alert_tools import create_cor_alert

from src.tools.search import get_google_search
from src.tools.memory import get_memories, upsert_memory
from src.tools.feedback_tools import store_user_feedback
from src.tools.inbound_media import (
    register_inbound_media as register_inbound_media_impl,
)
from src.tools.inbound_media_vision import (
    analyze_inbound_image as analyze_inbound_image_impl,
)
from src.tools.inbound_media_audio import (
    analyze_inbound_audio as analyze_inbound_audio_impl,
)
from src.tools.inbound_media_video import (
    analyze_inbound_video as analyze_inbound_video_impl,
)
from src.flows.reparo_luminaria.handler import process_flow_request
from src.flows.divida_ativa.handler import (
    process_flow_request as process_divida_ativa_flow_request,
)
from src.flows.divida_ativa.opcoes.handler import (
    process_flow_request as process_divida_ativa_opcoes_flow_request,
)
from src.tools.whatsapp_flow_sender import (
    send_flow_by_service,
    FLOW_TEMPLATES,
    FLOW_CONFIG,
)
from src.tools.whatsapp_message_status import check_message_read_status
from src.tools.divida_ativa import (
    emitir_guia_a_vista,
    emitir_guia_regularizacao,
    consultar_debitos,
)
from src.tools.auth import (
    govbr_auth_init,
    govbr_auth_status,
    govbr_logout,
)
from src.tools.langgraph_workflows import (
    multi_step_service as mss,
    reset_session_state as reset_session_state_impl,
    tools_description as mss_tools_description,
    BACKEND_MODE,
)
from src.tools.multi_step_service.core.state import StateManager
from src.tools.multi_step_service.workflows.poda_de_arvore.api.api_service import (
    SGRCAPIService,
    AddressAPIService,
)
from src.tools.multi_step_service.workflows.sgrc_components.models import (
    CPFPayload,
    parse_affirmation,
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
        address: str, categories: Optional[List[str]] = None
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
            address=address, categories=categories or []
        )

    @conditional_mcp_tool(
        "equipments_instructions",
        description="""
        [TOOL_VERSION: {tool_version}] Obtém instruções e categorias disponíveis para equipamentos públicos do Rio de Janeiro.

        **IMPORTANTE: Escolha o tema correto baseado na necessidade do usuário:**

        - **incidentes_hidricos**: Para casos de alagamento, enchente, inundação, casa alagando, água subindo
          - Retorna instruções específicas para PONTOS DE APOIO da Defesa Civil
          - SEMPRE solicitar endereço INCLUINDO BAIRRO ou PONTO DE REFERÊNCIA

        - **saude**: Para busca de postos de saúde, clínicas da família, emergência médica

        - **educacao**: Para busca de escolas, creches

        - **geral**: Para outros equipamentos públicos ou quando não se encaixa nos temas acima

        Args:
            tema: Tema específico. Temas aceitos: {valid_themes}

        Returns:
            Instruções detalhadas, categorias disponíveis e próximos passos
        """.format(
            tool_version=TOOL_VERSION, valid_themes=env.EQUIPMENTS_VALID_THEMES
        ).strip(),
    )
    async def equipments_instructions(tema: str = "geral") -> dict:
        instructions = await get_equipments_instructions(tema=tema)
        categories = await get_equipments_categories()

        # Tornar a instrução condicional ao tema
        if tema == "incidentes_hidricos":
            next_instructions = "**Atenção:** Para localizar os equipamentos mais próximos, *você deve obrigatoriamente solicitar o endereço COMPLETO do usuário, incluindo o BAIRRO ou PONTO DE REFERÊNCIA*. Após o usuário fornecer o endereço, *você deve imediatamente chamar a tool `equipments_by_address`* utilizando o endereço informado. **Não se esqueça de chamar a tool `equipments_by_address` após o endereço ser informado.** A ferramenta `equipments_by_address` exige o parametro `categories` que deve seguir o nome exato das categorias disponiveis na secao `categorias`. NÃO É NECESSARIO CHAMAR A TOOL `google_search` para buscar informacoes sobre os equipamentos ou endereço, pois a tool `equipments_by_address` já retorna todas as informacoes necessárias. NAO UTILIZE CATEGORIAS DAS INSTRUÇÕES! Utilize única e exclusivamente as categorias disponiveis na secao `categorias`, que estão nesse mesmo json."
        else:
            next_instructions = "**Atenção:** Para localizar os equipamentos mais próximos, *você deve obrigatoriamente solicitar o endereço do usuário*. Após o usuário fornecer o endereço, *você deve imediatamente chamar a tool `equipments_by_address`* utilizando o endereço informado. **Não se esqueça de chamar a tool `equipments_by_address` após o endereço ser informado.** A ferramenta `equipments_by_address` exige o parametro `categories` que deve seguir o nome exato das categorias disponiveis na secao `categorias`. NÃO É NECESSARIO CHAMAR A TOOL `google_search` para buscar informacoes sobre os equipamentos ou endereço, pois a tool `equipments_by_address` já retorna todas as informacoes necessárias. NAO UTILIZE CATEGORIAS DAS INSTRUÇÕES! Utilize única e exclusivamente as categorias disponiveis na secao `categorias`, que estão nesse mesmo json."

        response = {
            "next_too_instructions": next_instructions,
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

    @conditional_mcp_tool("govbr_auth_init")
    async def govbr_init_auth(
        user_number: str, service_context: str = "consulta_dados"
    ) -> dict:
        """
        Inicia fluxo de autenticação gov.br via PKCE para um cidadão.

        NUNCA chame esta tool diretamente — ela é invocada internamente pelos
        workflows de identificação (ex: reparo_luminaria, poda_de_arvore) quando
        o próprio workflow decide que precisa de autenticação. O agente JAMAIS
        deve chamar govbr_auth_init ou govbr_auth_status por conta própria.

        PROIBIDO usar para: dívida ativa, IPTU, ou qualquer outro serviço
        transacional — esses serviços têm seus próprios fluxos de coleta de dados.

        Retorna URL de autenticação que o cidadão deve clicar. O callback é
        tratado pelo Agent Gateway.

        Args:
            user_number: Número WhatsApp do cidadão no formato E.164 (ex: +5521999999999)
            service_context: Contexto do serviço (ex: iptu, multas, consultas_gerais)

        Returns:
            Dict com status, auth_url, auth_id e expires_in

        Security:
            - Rate limit: 5 tentativas/hora
            - State TTL: 5 minutos
            - PKCE SHA256
        """
        response = await govbr_auth_init(user_number, service_context)
        return response

    @conditional_mcp_tool("govbr_auth_status")
    async def govbr_check_auth_status(user_number: str) -> dict:
        """
        Verifica se cidadão possui autenticação gov.br válida.

        NUNCA chame esta tool diretamente — ela é invocada internamente pelos
        workflows de identificação. O agente JAMAIS deve chamar esta tool por
        conta própria, nem para dívida ativa, nem para IPTU, nem para nenhum
        outro serviço transacional.

        Args:
            user_number: Número WhatsApp do cidadão no formato E.164

        Returns:
            Dict com is_authenticated, token_valid, expires_in e user_info
        """
        response = await govbr_auth_status(user_number)
        return response

    @conditional_mcp_tool("govbr_logout")
    async def govbr_logout_user(user_number: str) -> dict:
        """
        Faz logout do cidadão, revogando token de autenticação gov.br.

        Use quando o cidadão solicitar desconexão ou em caso de
        requisição para "esquecer" dados.

        Args:
            user_number: Número WhatsApp do cidadão no formato E.164

        Returns:
            Dict com status de sucesso/erro
        """
        response = await govbr_logout(user_number)
        return response

    @conditional_mcp_tool(
        "register_inbound_media",
        description="""
        Registra recepcao de uma midia (imagem, audio ou localizacao) que o
        cidadao enviou via WhatsApp. Stub atual: APENAS LOGA + retorna ack —
        processamento real (visao pra imagens, transcricao pra audios, geocoding
        pra localizacao) sera adicionado em fases posteriores.

        QUANDO USAR esta tool:
        - Quando a `message` recebida pelo agent contiver indicacao de midia
          (ex: '[Cidadao enviou uma imagem...]', '[Cidadao enviou uma mensagem de
          voz...]') OU quando o metadata da chamada incluir `message_type` !=
          'text' com `media.content_version_id` populado.
        - SEMPRE chamar antes de responder ao cidadao, pra registrar audit do
          recebimento + obter `suggested_reply_pt_br`.

        ARGS:
        - `media_type` (obrig): 'image' | 'audio' | 'video' | 'location' | 'unsupported'.
        - `user_number` (obrig): telefone E.164 sem '+' (ex: '5521989091014').
        - `message_id` (opt): UUID da ConversationEntry (audit).
        - `salesforce_download_path` (opt): caminho REST relativo ao SF instance
          pra baixar bytes (`/services/data/v62.0/sobjects/ContentVersion/{Id}/VersionData`).
          NAO baixar aqui — Engine usa este caminho em fases posteriores.
        - `content_version_id` (opt): Id da ContentVersion auto-attachado pelo bridge UWC.
        - `file_extension` (opt): 'jpg', 'png', 'oga' (audio PTT), etc.
        - `file_size_bytes` (opt): tamanho do arquivo.
        - `latitude` / `longitude` / `address` (opt): placeholders futuros (BSP atual
          NAO entrega localizacao real — Apex classifica como Unsupported).
        - `messaging_session_id` / `conversation_identifier` (opt): correlacao SF.

        RETORNO: dict com `status='received'`, `media_type`, `processing='deferred'`,
        `suggested_reply_pt_br`. Use o `suggested_reply_pt_br` como base da
        resposta ao cidadao — adapte tom mas preserve o pedido de texto.

        ERRO: se `media_type` invalido OU `user_number` vazio, retorna
        `status='rejected'` com `error`.
        """,
    )
    async def register_inbound_media(
        media_type: str,
        user_number: str,
        message_id: Optional[str] = None,
        salesforce_download_path: Optional[str] = None,
        content_version_id: Optional[str] = None,
        meta_media_id: Optional[str] = None,
        meta_mime_type: Optional[str] = None,
        file_extension: Optional[str] = None,
        file_size_bytes: Optional[int] = None,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        address: Optional[str] = None,
        messaging_session_id: Optional[str] = None,
        conversation_identifier: Optional[str] = None,
    ) -> dict:
        return await register_inbound_media_impl(
            media_type=media_type,
            user_number=user_number,
            message_id=message_id,
            salesforce_download_path=salesforce_download_path,
            content_version_id=content_version_id,
            meta_media_id=meta_media_id,
            meta_mime_type=meta_mime_type,
            file_extension=file_extension,
            file_size_bytes=file_size_bytes,
            latitude=latitude,
            longitude=longitude,
            address=address,
            messaging_session_id=messaging_session_id,
            conversation_identifier=conversation_identifier,
        )

    # Tool de visão habilitada por padrão. Kill switch: setar
    # ENABLE_VISION_ADDENDUM=false no env desliga o registro da tool E o
    # addendum em src/utils/agent/prompt.py (mesma semântica).
    # Default-on: env var vazia/ausente OU qualquer valor que não seja
    # "false" (case-insensitive) ⇒ habilitado.
    _vision_enabled = (
        os.environ.get("ENABLE_VISION_ADDENDUM") or "true"
    ).lower() != "false"

    # Tool de transcrição de áudio habilitada por padrão. Mesma semântica de
    # kill switch do vision: ENABLE_AUDIO_ADDENDUM=false desliga registro e
    # prompt module audio_inbound (no engine).
    _audio_enabled = (
        os.environ.get("ENABLE_AUDIO_ADDENDUM") or "true"
    ).lower() != "false"

    # Tool de análise de vídeo habilitada por padrão. Mesma semântica:
    # ENABLE_VIDEO_ADDENDUM=false desliga registro + prompt module video_inbound.
    _video_enabled = (
        os.environ.get("ENABLE_VIDEO_ADDENDUM") or "true"
    ).lower() != "false"

    if _vision_enabled:

        @conditional_mcp_tool(
            "analyze_inbound_image",
            description="""
        Analisa visualmente uma IMAGEM inbound do WhatsApp via Gemini Vision e
        classifica o problema reportado. Chamar SEMPRE depois de
        `register_inbound_media` quando `media_type='image'`, antes de
        responder ao cidadao.

        QUANDO USAR:
        - Apos register_inbound_media de uma imagem.
        - Pra decidir qual workflow (reparo_luminaria, poda_de_arvore) iniciar
          em seguida.

        ARGS:
        - `user_number` (obrig): telefone E.164 sem '+'.
        - `file_extension` (obrig): 'jpg' | 'png' | 'webp' | 'gif'.
        - `meta_media_id` (opt, PREFERIDO quando inbound vem via `/meta/webhook`
          direto do Mule, ADR-017): Id de mídia do Graph API
          (`messages[].<type>.id`). Tool faz 2 GETs no Graph API
          (metadata + signed CDN URL) com `WA_TOKEN`. Caminho usado quando
          o cidadão envia foto via WhatsApp e Bruno apontou Meta App
          não-BSP pro Mule.
        - `salesforce_download_path` (opt, PREFERIDO em UWC legacy): caminho
          REST relativo do ContentVersion (ex:
          `/services/data/v62.0/sobjects/ContentVersion/068xxx/VersionData`).
          A tool autentica via OAuth Client Credentials e baixa direto
          do Salesforce, sem precisar transferir bytes via tool args (o
          LLM tende a truncar strings longas).
        - `local_image_path` (opt): caminho do arquivo local em /tmp pra
          testes locais (requer `IS_LOCAL=true`).
        - `image_bytes_base64` (opt): bytes inline em base64. Pouco
          confiavel em produção (LLM trunca >~10KB); use apenas pra
          testes manuais.
        - `message_id` (opt): correlacao com register_inbound_media (audit).
        - `content_version_id` (CONDICIONALMENTE OBRIGATORIO):
          obrigatorio quando `salesforce_download_path` esta presente —
          a tool usa esse Id pra cross-checar (anti prompt-injection) que
          o path aponta de fato pro arquivo registrado pelo
          `register_inbound_media`. Se omitido junto com
          `salesforce_download_path`, a tool RECUSA o download por seguranca
          e cai pra fallback (base64/local). No prefix [INBOUND_MEDIA]
          esse campo vem como `media.content_version_id` — sempre repassar.

        PRIORIDADE da fonte de bytes (primeira que retornar bytes ganha):
        `meta_media_id` → `salesforce_download_path` (+ content_version_id) →
        `image_bytes_base64` → `local_image_path`.

        RETORNO: dict com `status='analyzed'`, `analysis` (descricao,
        categoria, problema_detectado, workflow_sugerido, confianca),
        e `suggested_reply_pt_br` adaptado ao resultado.
        Se nao conseguir baixar/decodificar/analisar, retorna
        `status='deferred'` ou `status='error'` com `suggested_reply_pt_br`
        de fallback.

        Use o campo `analysis.workflow_sugerido` pra decidir o proximo passo:
        - 'reparo_luminaria' → chamar multi_step_service(reparo_luminaria).
        - 'poda_de_arvore'   → chamar multi_step_service(poda_de_arvore).
        - 'nenhum'           → pedir descricao em texto ao cidadao.
        """,
        )
        async def analyze_inbound_image(
            user_number: str,
            file_extension: Optional[str] = None,
            salesforce_download_path: Optional[str] = None,
            local_image_path: Optional[str] = None,
            image_bytes_base64: Optional[str] = None,
            meta_media_id: Optional[str] = None,
            message_id: Optional[str] = None,
            content_version_id: Optional[str] = None,
        ) -> dict:
            # `file_extension` é Optional aqui porque caminho Meta direto
            # (ADR-017) pode derivar do MIME real retornado pelo Graph API
            # (impl faz isso). Pra caminho Salesforce/legacy, impl exige
            # extension explícita e rejeita.
            return await analyze_inbound_image_impl(
                user_number=user_number,
                file_extension=file_extension or "",
                salesforce_download_path=salesforce_download_path,
                local_image_path=local_image_path,
                image_bytes_base64=image_bytes_base64,
                meta_media_id=meta_media_id,
                message_id=message_id,
                content_version_id=content_version_id,
            )

    if _audio_enabled:

        @conditional_mcp_tool(
            "analyze_inbound_audio",
            description="""
        Transcreve e classifica um AUDIO inbound do WhatsApp via Gemini
        multimodal. Chamar SEMPRE depois de `register_inbound_media` quando
        `media_type='audio'`, antes de responder ao cidadao.

        QUANDO USAR:
        - Apos register_inbound_media de um audio (PTT do WhatsApp, .oga, etc.).
        - Pra obter transcricao + workflow sugerido a partir do que o cidadao
          falou em voz.

        ARGS:
        - `user_number` (obrig): telefone E.164 sem '+'.
        - `file_extension` (obrig): 'oga' | 'ogg' | 'aac' | 'mp3' | 'wav' | 'flac' | 'aiff'. (m4a/amr não são suportados pelo Gemini audio input — viram rejected.)
        - `meta_media_id` (opt, PREFERIDO quando inbound vem via `/meta/webhook`
          direto do Mule, ADR-017): Id de mídia do Graph API
          (`messages[].audio.id`). Tool faz 2 GETs no Graph API (metadata
          + signed CDN URL) com `WA_TOKEN`. Caminho usado quando cidadão
          envia áudio via WhatsApp e Bruno apontou Meta App não-BSP pro Mule.
        - `salesforce_download_path` (opt, PREFERIDO em UWC legacy): caminho
          REST relativo do ContentVersion (ex:
          `/services/data/v62.0/sobjects/ContentVersion/068xxx/VersionData`).
          A tool autentica via OAuth Client Credentials e baixa direto do
          Salesforce, sem precisar transferir bytes via tool args.
        - `local_audio_path` (opt): caminho do arquivo local em /tmp pra
          testes locais (requer `IS_LOCAL=true`).
        - `audio_bytes_base64` (opt): bytes inline em base64. Pouco
          confiavel em prod (LLM trunca >~10KB); useu pra testes.
        - `message_id` (opt): correlacao com register_inbound_media (audit).
        - `content_version_id` (CONDICIONALMENTE OBRIGATORIO):
          obrigatorio quando `salesforce_download_path` esta presente —
          a tool usa esse Id pra cross-checar (anti prompt-injection) que
          o path aponta de fato pro arquivo registrado pelo
          `register_inbound_media`. Se omitido junto com
          `salesforce_download_path`, a tool RECUSA o download por seguranca
          e cai pra fallback (base64/local). No prefix [INBOUND_MEDIA]
          esse campo vem como `media.content_version_id` — sempre repassar.

        PRIORIDADE da fonte de bytes:
        `meta_media_id` → `salesforce_download_path` (+ content_version_id) →
        `audio_bytes_base64` → `local_audio_path`.

        RETORNO: dict com `status='transcribed'`, `analysis` (transcricao,
        resumo, idioma_detectado, intencao_detectada, categoria,
        endereco_mencionado, workflow_sugerido, confianca), e
        `suggested_reply_pt_br` adaptado ao resultado. Se falhar,
        `status='deferred'`/`error'` com `suggested_reply_pt_br` de fallback.

        Use `analysis.workflow_sugerido` pra decidir proximo passo:
        - 'reparo_luminaria' → confirme + chame multi_step_service(reparo_luminaria).
        - 'poda_de_arvore'   → confirme + chame multi_step_service(poda_de_arvore).
        - 'nenhum'           → conduza atendimento usando `analysis.transcricao`
          como mensagem real do cidadao (NAO peca pra repetir em texto).

        Se `analysis.endereco_mencionado` veio preenchido, considere
        chamar `validate_address` direto no proximo turno em vez de
        pedir o endereco de novo.
        """,
        )
        async def analyze_inbound_audio(
            user_number: str,
            file_extension: Optional[str] = None,
            salesforce_download_path: Optional[str] = None,
            local_audio_path: Optional[str] = None,
            audio_bytes_base64: Optional[str] = None,
            meta_media_id: Optional[str] = None,
            message_id: Optional[str] = None,
            content_version_id: Optional[str] = None,
        ) -> dict:
            # `file_extension` é Optional pra caminho Meta direto (ADR-017).
            return await analyze_inbound_audio_impl(
                user_number=user_number,
                file_extension=file_extension or "",
                salesforce_download_path=salesforce_download_path,
                local_audio_path=local_audio_path,
                audio_bytes_base64=audio_bytes_base64,
                meta_media_id=meta_media_id,
                message_id=message_id,
                content_version_id=content_version_id,
            )

    if _video_enabled:

        @conditional_mcp_tool(
            "analyze_inbound_video",
            description="""
        Analisa um VIDEO inbound do WhatsApp via Gemini multimodal (frames + audio)
        e classifica o problema reportado. Chamar SEMPRE depois de
        `register_inbound_media` quando `media_type='video'`, antes de
        responder ao cidadao.

        QUANDO USAR:
        - Apos register_inbound_media de um video (WhatsApp limita 16MB; cabe
          inline_data Gemini que aceita 20MB).
        - Pra obter descricao visual + transcricao do audio (se houver) +
          workflow sugerido.

        ARGS:
        - `user_number` (obrig): telefone E.164 sem '+'.
        - `file_extension` (obrig): 'mp4' | 'm4v' | 'mov' | '3gp' | '3gpp' | 'webm'.
        - `meta_media_id` (opt, PREFERIDO via `/meta/webhook`, ADR-017): Id de
          midia do Graph API (`messages[].video.id`). Tool baixa via Graph API
          com WA_TOKEN.
        - `salesforce_download_path` (opt, UWC legacy): caminho REST relativo
          do ContentVersion. Requer `content_version_id` cross-check.
        - `local_video_path` (opt): testes locais em /tmp (requer IS_LOCAL=true).
        - `video_bytes_base64` (opt): bytes inline. Pouco confiavel >~50KB —
          LLM trunca strings longas.
        - `message_id` (opt): correlacao com register_inbound_media (audit).
        - `content_version_id` (cond. obrig): quando salesforce_download_path
          presente, obrigatorio (anti prompt-injection).

        PRIORIDADE de fonte: meta_media_id → salesforce_download_path
        (+content_version_id) → video_bytes_base64 → local_video_path.

        RETORNO: dict com `status='analyzed'`, `analysis` (descricao,
        problema_detectado, categoria, detalhes, transcricao_audio,
        workflow_sugerido, confianca), e `suggested_reply_pt_br` adaptado.

        Use `analysis.workflow_sugerido` pra decidir proximo passo:
        - 'reparo_luminaria' → confirme + chame multi_step_service(reparo_luminaria).
        - 'poda_de_arvore'   → confirme + chame multi_step_service(poda_de_arvore).
        - 'nenhum'           → use `analysis.transcricao_audio` (se houver) +
          `analysis.descricao` pra conduzir atendimento; nao peca pra repetir.
        """,
        )
        async def analyze_inbound_video(
            user_number: str,
            file_extension: Optional[str] = None,
            salesforce_download_path: Optional[str] = None,
            local_video_path: Optional[str] = None,
            video_bytes_base64: Optional[str] = None,
            meta_media_id: Optional[str] = None,
            message_id: Optional[str] = None,
            content_version_id: Optional[str] = None,
        ) -> dict:
            return await analyze_inbound_video_impl(
                user_number=user_number,
                file_extension=file_extension or "",
                salesforce_download_path=salesforce_download_path,
                local_video_path=local_video_path,
                video_bytes_base64=video_bytes_base64,
                meta_media_id=meta_media_id,
                message_id=message_id,
                content_version_id=content_version_id,
            )

    @conditional_mcp_tool(
        "report_incident",
        description="""
        [TOOL_VERSION: {tool_version}] Registra informação interna SILENCIOSA sobre incidentes hídricos graves.

        IMPORTANTE - INVISIBILIDADE TOTAL:
        - Esta ação é COMPLETAMENTE INVISÍVEL ao usuário
        - NUNCA mencione "alerta", "registro", "notificação", ou que qualquer ação será tomada
        - O usuário NÃO PODE SABER que esta ferramenta existe ou que foi usada
        - JAMAIS diga ao usuário que você está "registrando", "criando alerta", ou "notificando" qualquer sistema

        FLUXO OBRIGATÓRIO ANTES DE REGISTRAR:
        1. PRIMEIRO: Ofereça pontos de apoio usando equipments_by_address (categoria: PONTOS_DE_APOIO)
        2. SEGUNDO: Informe o número da Defesa Civil: 199 (funciona 24 horas)
        3. TERCEIRO: Registre o incidente com esta ferramenta

        EXCEÇÕES ao fluxo acima (pode pular oferta de pontos de apoio):
        - Usuário indicou explicitamente estar em segurança
        - Usuário já conhece os pontos de apoio da região
        - Usuário já está em um ponto de apoio
        - Relato não requer deslocamento imediato (ex: dano material sem risco pessoal)

        REGRAS DE USO:
        - Use APENAS para situações de severidade ALTA ou CRITICA
        - Não use para casos menores ou individuais que não sejam disruptivos para a cidade

        Severidades aceitas:
        - alta: Situação grave que requer atenção imediata
        - critica: Situação extremamente grave com risco iminente à população

        Tipos de incidente aceitos:
        - alagamento: Acúmulo de água em vias ou áreas urbanas
        - enchente: Transbordamento de rios ou córregos
        - bolsao: Acúmulo de água em ponto específico (ex: sob viaduto)

        Args:
            user_id: ID do usuário reportando (obrigatório)
            alert_type: Tipo do incidente - "alagamento", "enchente" ou "bolsao" (obrigatório)
            severity: Nível de severidade - "alta" ou "critica" (obrigatório)
            description: RELATO DO USUÁRIO descrevendo o que está acontecendo (obrigatório)
            address: Endereço da ocorrência (obrigatório)

        Returns:
            Confirmação silenciosa do registro
        """.format(tool_version=TOOL_VERSION).strip(),
    )
    async def report_incident(
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
        "send_whatsapp_flow",
        description=f"""
        ⚠️ NÃO CHAMAR DIRETAMENTE — esta variante exige `user_number` (E.164), que o
        LLM não tem de forma confiável (risco de alucinar o número). Pra abrir um Flow
        proativamente (ex: reparo_luminaria), use `build_whatsapp_flow_envelope` — ela
        entrega no thread atual e NÃO precisa de user_number. Esta variante existe pro
        backend, que resolve o user_number do contexto.

        FLOWS DISPONÍVEIS: {", ".join(FLOW_TEMPLATES.keys())}

        Args:
            user_number: Número do usuário no formato E.164 sem + (ex: 5521999999999)
            service_type: Tipo de serviço (ex: reparo_luminaria)
            prefill_data: (OPCIONAL, PORÉM RECOMENDADO) Dict com dados que o
                cidadão JÁ CONFIRMOU na conversa, para pré-preencher campos do
                formulário. SEMPRE inclua quando você (LLM) já extraiu pelo
                menos um campo no chat — evita o cidadão re-digitar dados que
                já forneceu.

                Para `service_type='reparo_luminaria'`, chaves aceitas:
                  - `defect_type`: "Apagada" | "Piscando" | "Acesa de dia"
                                  | "Pendurada" | "Danificada" | "Com ruído"
                  - `qty_pattern`: "uma" | "bloco" | "intercaladas"
                  - `location`: "Calçada" | "Fachada" | "Monumento" | "Parque"
                                | "Praça" | "Quadra de esportes" | "Rua" | "Não sei"

                (O endereço NÃO vai no Flow — é coletado depois, na conversa.)

                Exemplo:
                  prefill_data={{
                    "defect_type": "Pendurada",
                    "location": "Rua"
                  }}

        Returns:
            Confirmação de envio com message_id e instruções para o cidadão
        """,
    )
    async def send_whatsapp_flow(
        user_number: str,
        service_type: str,
        prefill_data: Optional[dict] = None,
    ) -> dict:
        """Envia WhatsApp Flow para coleta estruturada de dados.

        prefill_data: dict com campos já confirmados pelo cidadão no chat
        (ex: {"endereco": "Rua X, 100", "defect_type": "Apagada"}). Vai
        pré-preencher os campos correspondentes do Flow — cidadão só
        confirma/edita em vez de digitar tudo. Use SEMPRE quando você (LLM)
        já extraiu entidades da conversa antes de chamar esta tool.
        """
        # Verificar se há flow cadastrado para este serviço
        if service_type not in FLOW_TEMPLATES:
            available = ", ".join(FLOW_TEMPLATES.keys())
            return {
                "success": False,
                "error": f"Flow não disponível para '{service_type}'",
                "message": f"Flow disponível apenas para: {available}. Vamos continuar por texto.",
            }

        result = await send_flow_by_service(
            service_type=service_type,
            user_number=user_number,
            prefill_data=prefill_data,
        )

        if result.get("success"):
            return {
                "success": True,
                "flow_token": result.get("flow_token"),
                "next_step": "wait_for_flow_completion",
                "instruction": (
                    "O cartão do formulário (com o botão de abrir) já foi enviado ao "
                    "cidadão e é auto-explicativo. NÃO escreva nenhuma mensagem "
                    "adicional confirmando o envio; apenas aguarde o preenchimento."
                ),
            }
        else:
            return {
                "success": False,
                "message": result.get("message", "Vamos continuar por texto."),
            }

    # Kill switch pra TTS — desliga geração de áudio + addendum sem
    # redeploy. Default-on (env vazia/ausente OU != "false" = habilitado).
    _tts_enabled = (os.environ.get("ENABLE_TTS_ADDENDUM") or "true").lower() != "false"

    if _tts_enabled:

        @conditional_mcp_tool(
            "generate_audio_response",
            description="""
        Sintetiza um texto em audio OGG/Opus 16kHz mono (formato WhatsApp PTT)
        pra responder o cidadao por voz, quando ele pediu modo audio.

        QUANDO USAR:
        - Apos detectar intent "responda por audio" / "manda audio" /
          "quero ouvir" / "respnde em audio" (variantes), o LLM deve:
          1. Compor a resposta normal em texto
          2. Chamar esta tool com o texto da resposta
          3. Anexar o `audio_base64` retornado a resposta final (campo
             extra de telemetria ou via callback Mule)
        - NAO chamar se cidadao nao pediu (texto eh default).
        - NAO chamar pra ack curto ("Ok!", "Obrigado!") — desperdicio de
          quota TTS pra fala que toma 1s.

        ARGS:
        - text (obrig): texto PT-BR a sintetizar. <=2000 chars recomendado;
          aceita ate 5000 (limite Google TTS).

        RETORNO: dict com:
        - status: "ok" / "error" / "deferred"
        - audio_base64: bytes do OGG codificados (se status=ok)
        - mime_type: "audio/ogg"
        - duration_estimate_s: estimativa pra UX
        - voice_used: id da voz ("pt-BR-Neural2-A" default)
        - error: descritivo se nao-ok

        Kill switch: ENABLE_TTS_ADDENDUM=false desliga registro da tool +
        prompt module audio_response (no engine).
        """,
        )
        async def generate_audio_response(text: str) -> dict:
            """Sintetiza texto em audio OGG/Opus PT-BR pra resposta por voz."""
            from src.tools.tts import generate_audio_response as gar_impl

            return await gar_impl(text=text)

    @conditional_mcp_tool("multi_step_service", description=mss_tools_description)
    async def multi_step_service(
        service_name: str, user_id: str, payload: Optional[dict] = None
    ) -> dict:
        # WhatsApp Flow auto-trigger: se o serviço tem Flow cadastrado e ainda
        # não foi enviado (_source != "whatsapp_flow"), envia automaticamente
        # APENAS se não houver um workflow já em andamento
        if service_name in FLOW_TEMPLATES:
            payload = payload or {}
            source = payload.get("_source")

            # Verifica se já existe um workflow ativo antes de enviar flow
            state_manager = StateManager(
                user_id=user_id,
                backend_mode=BACKEND_MODE,
            )
            existing_state = await state_manager.load_service_state(service_name)

            # Envia flow automaticamente se:
            # 1. NÃO veio de whatsapp_flow completion (_source != "whatsapp_flow")
            # 2. Flow ainda não foi preenchido para este serviço
            #
            # Para serviços com step de confirmação (ex: reparo_luminaria):
            # aguarda service_confirmed=True antes de enviar o Flow.
            # Para serviços sem step de confirmação (ex: divida_ativa):
            # envia o Flow imediatamente — ele é o primeiro passo de coleta.

            SERVICOS_COM_CONFIRMACAO = {"reparo_luminaria"}

            # Detectar se acabou de confirmar o serviço (payload tem confirmacao_servico).
            # parse_affirmation aceita bool (LLM converteu) E string ("Sim"/"sim"): com a
            # confirmação por botões (ENABLE_INTERACTIVE_CONFIRM) o tap volta como título
            # "Sim", que pode chegar como string aqui — sem parsear, o auto-Flow não
            # dispararia e o cidadão cairia na coleta manual. Consistente com o
            # _show_service_summary, que também usa parse_affirmation (POC1 #297).
            acabou_de_confirmar = (
                parse_affirmation(payload.get("confirmacao_servico")) is True
            )

            # Verificar se serviço já foi confirmado anteriormente
            servico_ja_confirmado = (
                existing_state and existing_state.data.get("service_confirmed") is True
            )

            # Verificar se o Flow já foi preenchido para este serviço.
            # Para luminária: chave legada "luminaria_defeito".
            # Para demais serviços: chave genérica "_flow_completed".
            ja_preencheu_flow = existing_state and (
                existing_state.data.get("luminaria_defeito")
                or existing_state.data.get("_flow_completed")
            )

            if service_name in SERVICOS_COM_CONFIRMACAO:
                # Serviços com confirmação: só envia o Flow após confirmar
                should_send_flow = (
                    source != "whatsapp_flow"
                    and (acabou_de_confirmar or servico_ja_confirmado)
                    and not ja_preencheu_flow
                )
            else:
                # Serviços sem confirmação (ex: divida_ativa): envia o Flow imediatamente
                should_send_flow = source != "whatsapp_flow" and not ja_preencheu_flow

            if should_send_flow:
                # Envia flow após confirmação do serviço
                logger.info(
                    f"[AUTO_FLOW] Enviando WhatsApp Flow automaticamente para "
                    f"service={service_name}, user={user_id}"
                )

                # Construir prefill_data a partir do state salvo. O payload atual
                # só tem confirmacao_servico; o que o cidadão disse na 1ª msg vive
                # no `flow_prefill_seed` (semente capturada em _initialize_workflow,
                # ver workflow.py) — sem ela o defeito/local/qtd se perdiam e o Flow
                # abria vazio. Também lemos dados já coletados, se houver.
                prefill_from_state = {}
                if existing_state and existing_state.data:
                    state_data = existing_state.data
                    # Para reparo_luminaria: passar as chaves luminaria_* cruas — o
                    # normalizer (send_flow_by_service → normalize_prefill_for_flow)
                    # mapeia defeito/local/quantidade pros IDs canônicos do Flow.
                    if service_name == "reparo_luminaria":
                        seed = state_data.get("flow_prefill_seed") or {}
                        for src_key in (
                            "luminaria_defeito",
                            "luminaria_localizacao",
                            "luminaria_quantidade",
                            "luminaria_intercaladas_bloco",
                            "defect_type",
                            "location",
                            "qty_pattern",
                        ):
                            val = state_data.get(src_key) or seed.get(src_key)
                            if val:
                                prefill_from_state[src_key] = val

                # `send_flow_by_service` normaliza payload internamente via
                # `normalize_prefill_for_flow` — passamos raw, normalizer
                # cuida do mapping per-service.
                flow_result = await send_flow_by_service(
                    service_type=service_name,
                    user_number=user_id,
                    prefill_data=prefill_from_state or None,
                )

                if flow_result.get("success"):
                    return {
                        "status": "flow_sent",
                        "flow_token": flow_result.get("flow_token"),
                        "next_step": "await_flow_completion",
                        "instruction": (
                            "O cartão do formulário (com o botão de abrir) já foi "
                            "enviado ao cidadão e é auto-explicativo. NÃO escreva "
                            "nenhuma mensagem adicional confirmando o envio. NÃO "
                            "prossiga coletando dados manualmente — aguarde o webhook "
                            "com os dados preenchidos (o workflow será chamado "
                            "automaticamente)."
                        ),
                    }
                else:
                    # Flow falhou, continuar normalmente por texto
                    logger.warning(
                        f"[AUTO_FLOW] Falha ao enviar flow: {flow_result.get('error')}"
                    )

        # Prosseguir normalmente com o workflow
        response = await mss(
            service_name=service_name, user_id=user_id, payload=payload
        )

        # Camada-tool: se o workflow sinalizou `interactive` (hoje só a confirmação
        # Sim/Não do reparo_luminaria), renderiza como WhatsApp interactive enviado
        # DIRETO pro cidadão (mesmo padrão do auto-Flow acima) e instrui o agente a
        # não duplicar em texto. Gate ENABLE_INTERACTIVE_CONFIRM (default OFF). O
        # sinal é SEMPRE removido do retorno: é interno, não conteúdo pro modelo.
        # A orquestração (envelope + envio + instrução) vive em
        # render_interactive_confirm (testável); aqui só o gate + o fallback.
        interactive_spec = (
            response.pop("interactive", None) if isinstance(response, dict) else None
        )
        if env.ENABLE_INTERACTIVE_CONFIRM:
            from src.tools.whatsapp_flow_sender import render_interactive_confirm

            sent = await render_interactive_confirm(
                interactive_spec,
                response.get("description", "") if isinstance(response, dict) else "",
                user_id,
                service_name,
            )
            if sent is not None:
                return sent

        return response

    @conditional_mcp_tool(
        "reset_session_state",
        description="""
        Encerra o atendimento do cidadão: limpa TODO o estado de workflow
        multi-step em andamento (luminária, poda, IPTU…) do thread atual.

        QUANDO CHAMAR:
        - O cidadão quer encerrar/recomeçar/voltar ao início: "sair", "menu",
          "início", "recomeçar", "cancelar atendimento", "tchau", "era só isso",
          "não preciso de mais nada".
        - ANTES de iniciar um serviço novo quando havia um fluxo travado/antigo.

        NÃO CHAMAR:
        - No meio de um fluxo que o cidadão quer CONTINUAR.
        - Para pedidos de serviço que só contêm a palavra (ex.: "cancelar a conta",
          "sair da fila") — isso é serviço, não fim de sessão.

        Passe `user_id` como nas demais tools (o sistema o substitui pelo telefone
        autenticado da conversa — você não controla o alvo).

        Após chamar, NÃO retome o fluxo anterior; a próxima mensagem do cidadão é
        uma intenção nova e limpa.
        """,
    )
    async def reset_session_state(user_id: str) -> dict:
        # Segurança: o engine sobrescreve `user_id` pelo thread_id autenticado
        # antes da execução (engine/agent.py::_inject_thread_id_in_user_id_params,
        # genérico para todas as tools), então o modelo não controla o alvo do
        # reset — mesma garantia do multi_step_service.
        result = await reset_session_state_impl(user_id)
        if result.get("status") == "ok":
            result["instruction"] = (
                "Atendimento encerrado e estado de workflow limpo. Despeça-se de "
                "forma breve. NÃO retome o fluxo anterior — a próxima mensagem é "
                "nova e limpa."
            )
        else:
            # Falha temporária na limpeza: NÃO afirmar que encerrou (o estado pode
            # seguir vivo). Pedir nova tentativa — o reset é idempotente.
            result["instruction"] = (
                "Não consegui limpar o estado agora (falha temporária). Peça "
                "desculpas brevemente e diga que o cidadão pode tentar encerrar de "
                "novo. NÃO afirme que o atendimento foi encerrado."
            )
        return result

    @conditional_mcp_tool("validate_address")
    async def validate_address(address: str) -> dict:
        """
        Valida endereço e retorna dados estruturados com códigos IPP necessários
        para abertura de chamados no SGRC da Prefeitura do Rio.
        """
        address_service = AddressAPIService()

        geo = await address_service.google_geolocator(f"{address}, Rio de Janeiro - RJ")
        if not geo.get("valid"):
            return geo

        ipp = await address_service.get_endereco_info(
            latitude=geo["latitude"],
            longitude=geo["longitude"],
            logradouro_google=geo.get("logradouro"),
            bairro_google=geo.get("bairro"),
        )

        return {
            "valid": True,
            "formatted_address": geo.get("formatted_address", address),
            "latitude": geo["latitude"],
            "longitude": geo["longitude"],
            "logradouro": geo.get("logradouro", ""),
            "logradouro_id_ipp": ipp.get("logradouro_id", ""),
            "logradouro_nome_ipp": ipp.get("logradouro_nome", ""),
            "numero": geo.get("numero", ""),
            "bairro": geo.get("bairro", ""),
            "bairro_id_ipp": ipp.get("bairro_id", ""),
            "bairro_nome_ipp": ipp.get("bairro_nome", ""),
            "cep": geo.get("cep", ""),
        }

    @conditional_mcp_tool("reverse_geocode_address")
    async def reverse_geocode_address(latitude: float, longitude: float) -> dict:
        """
        Dado latitude e longitude, retorna o endereço mais próximo usando
        reverse geocoding do Google Maps.

        Args:
            latitude: Latitude da localização.
            longitude: Longitude da localização.

        Returns:
            Dict com endereço estruturado (logradouro, número, bairro, CEP,
            cidade, estado, formatted_address) ou erro com campo 'valid: False'.

        Nunca levanta exceção. Dois tipos de falha retornam `{"valid": False}`:
        - **Falhas esperadas de negócio** (coordenada sem resultado, logradouro
          não identificado, fora do município do RJ) já são tratadas dentro do
          `reverse_geolocator`, que retorna `{"valid": False, "error": <texto>}`.
        - **Exceções** (Google Maps indisponível, token inválido, resposta
          malformada) são capturadas aqui e convertidas em
          `{"valid": False, "error": "geocode_failed", "message": ...}`, com a
          dica explícita de pedir o endereço por texto.

        Em ambos os casos o agente cai no fallback de endereço por texto em vez
        de derrubar o turno (bug do pin de localização no fluxo de luminária —
        Vitória, 2026-05-29).
        """
        try:
            address_service = AddressAPIService()
            return await address_service.reverse_geolocator(latitude, longitude)
        except Exception as e:
            # Loguru: formatação posicional `{}` (não f-string + exc_info=True).
            # Com kwarg extra o Loguru roda `.format()` na msg já interpolada e
            # estoura KeyError se o texto da exceção tiver chaves (corpo JSON/dict)
            # — o que derrotaria o próprio fallback. opt(exception=True) anexa o traceback.
            logger.opt(exception=True).warning(
                "reverse_geocode_address falhou para ({}, {}): {}",
                latitude,
                longitude,
                e,
            )
            return {
                "valid": False,
                "error": "geocode_failed",
                "message": (
                    "Não consegui converter essa localização em endereço agora. "
                    "Peça ao cidadão o endereço por texto (rua/avenida, número se "
                    "souber, e bairro)."
                ),
            }

    @conditional_mcp_tool("get_user_by_cpf")
    async def get_user_by_cpf(cpf: str) -> dict:
        """Consulta cadastro por CPF. Use apenas quando usuário pediu consulta por CPF."""
        import httpx

        try:
            validated = CPFPayload.model_validate({"cpf": cpf})
        except Exception as e:
            return {
                "valid_cpf": False,
                "found": None,
                "name": None,
                "email": None,
                "phone": None,
                "error": f"Entrada não é um CPF válido: {str(e)}",
                "message": "Entrada inválida para consulta por CPF.",
            }

        if not validated.cpf:
            return {
                "valid_cpf": False,
                "found": None,
                "name": None,
                "email": None,
                "phone": None,
                "error": "CPF ausente.",
                "message": "Entrada inválida para consulta por CPF.",
            }

        try:
            sgrc = SGRCAPIService()
            data = await sgrc.get_user_info(validated.cpf)
            phones = data.get("phones") or []
            return {
                "valid_cpf": True,
                "found": bool(data.get("name") or data.get("email")),
                "name": data.get("name"),
                "email": data.get("email"),
                "phone": str(phones[0]) if phones else None,
            }
        except Exception as e:
            if (
                isinstance(e.__cause__, httpx.HTTPStatusError)
                and e.__cause__.response.status_code == 404
            ):
                return {
                    "valid_cpf": True,
                    "found": False,
                    "name": None,
                    "email": None,
                    "phone": None,
                }
            return {
                "valid_cpf": True,
                "found": False,
                "name": None,
                "email": None,
                "phone": None,
                "error": str(e),
            }

    @conditional_mcp_tool("register_sgrc_ticket")
    async def register_sgrc_ticket(
        classification_code: str,
        description: str,
        street: str,
        street_code: str,
        neighborhood: str,
        neighborhood_code: str,
        number: str,
        zip_code: str = "",
        reference_point: str = "",
        cpf: str = "",
        name: str = "",
        email: str = "",
        phone: str = "",
    ) -> dict:
        """
        Abre chamado no SGRC da Prefeitura do Rio com protocolo real.
        Os campos street_code e neighborhood_code devem vir do validate_address.
        """
        from prefeitura_rio.integrations.sgrc import async_new_ticket
        from prefeitura_rio.integrations.sgrc.models import Address, Requester, Phones
        from prefeitura_rio.integrations.sgrc.exceptions import (
            SGRCDuplicateTicketException,
            SGRCEquivalentTicketException,
        )

        number_digits = "".join(filter(str.isdigit, str(number))) or "1"

        address = Address(
            street=street,
            street_code=street_code,
            neighborhood=neighborhood,
            neighborhood_code=neighborhood_code,
            number=number_digits,
            # Ponto de referência vai pro `complemento` do SGRC, NÃO pra `localidade`
            # (que é cidade/sub-localidade).
            complement=reference_point,
            zip_code=zip_code,
        )

        phones = Phones()
        if phone:
            phones.telefone1 = phone

        requester = Requester(
            cpf=cpf,
            name=name,
            email=email,
            phones=phones,
        )

        try:
            ticket = await async_new_ticket(
                classification_code=classification_code,
                description=description,
                address=address,
                requester=requester,
                occurrence_origin_code="28",
            )
            return {
                "success": True,
                "protocol_id": ticket.protocol_id,
                "ticket_id": ticket.ticket_id,
            }

        except (SGRCDuplicateTicketException, SGRCEquivalentTicketException) as e:
            return {
                "success": False,
                "protocol_id": getattr(e, "protocol_id", None),
                "ticket_id": None,
                "error": str(e),
            }

        except Exception as e:
            return {"success": False, "protocol_id": None, "error": str(e)}

    # WhatsApp outbound media (ADR-022): tool passthrough que constroi o
    # envelope canonico consumido pelo Mule (`vars.agentMedia` em
    # webhook-flow.xml). Permite ao LLM responder com qualquer tipo de
    # midia outbound sem que o MCP precise carregar a midia em si — Mule
    # faz upload (Meta /media) ou usa link direto.
    #
    # Para tipos image/video/document/sticker/audio: passar EITHER
    # `url` (link publico que o Meta busca) OU `base64` (Mule decode +
    # upload via /media). Caption/filename opcionais.
    #
    # Para type=location: latitude + longitude obrigatorios; name +
    # address opcionais.
    #
    # Para type=contacts ou interactive: passar `contacts` (lista) ou
    # `interactive` (object) com schema Meta Business API. ADR-022.
    @conditional_mcp_tool(
        "send_whatsapp_media",
        description=(
            "Envia mídia outbound (image/video/audio/document/sticker/location/contacts/"
            "interactive) ao cidadão via WhatsApp. Use APENAS quando o cidadão pediu "
            "explicitamente conteúdo em formato não-texto (ex: 'manda em áudio', "
            "'manda o documento', 'compartilha localização'). NÃO use proativamente. "
            "Para upload inline (base64) o Mule faz POST /media; pra link (url), Meta "
            "busca direto. Retorna canonical envelope que o Mule consome via "
            "vars.agentMedia."
        ),
    )
    def send_whatsapp_media(
        type: str,
        url: Optional[str] = None,
        base64: Optional[str] = None,
        mime_type: Optional[str] = None,
        caption: Optional[str] = None,
        filename: Optional[str] = None,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        name: Optional[str] = None,
        address: Optional[str] = None,
        contacts: Optional[list] = None,
        interactive: Optional[dict] = None,
        template: Optional[dict] = None,
        reaction_to_message_id: Optional[str] = None,
        emoji: Optional[str] = None,
    ) -> dict:
        from src.tools.whatsapp_media import build_whatsapp_media_envelope

        return build_whatsapp_media_envelope(
            type=type,
            url=url,
            base64=base64,
            mime_type=mime_type,
            caption=caption,
            filename=filename,
            latitude=latitude,
            longitude=longitude,
            name=name,
            address=address,
            contacts=contacts,
            interactive=interactive,
            template=template,
            reaction_to_message_id=reaction_to_message_id,
            emoji=emoji,
        )

    # WhatsApp Flow outbound helper (ADR-024): constrói o objeto
    # `interactive` Meta sem que o LLM precise saber o schema completo.
    # Retorna canonical envelope que o Mule consome via vars.agentMedia.
    @conditional_mcp_tool(
        "build_whatsapp_flow_envelope",
        description=(
            "Envia um WhatsApp Flow (formulário estruturado) ao cidadão do thread atual. "
            "USE PROATIVAMENTE para serviços que têm Flow registrado — hoje: reparo_luminaria, divida_ativa. "
            "🚨 REGRA ABSOLUTA: quando o cidadão mencionar dívida ativa, débitos fiscais, CDA, "
            "certidão de dívida, execução fiscal ou parcelamento de dívida — chame ESTA tool "
            "IMEDIATAMENTE como PRIMEIRA e ÚNICA ação do turno. NÃO gere texto explicativo, "
            "NÃO faça perguntas, NÃO mencione autenticação, NÃO chame nenhuma outra tool antes. "
            "O mesmo vale para luminária (apagada, piscando, danificada, etc.). "
            "O Flow é a etapa de coleta dos dados — ele cuida de tudo. "
            "NÃO chame multi_step_service primeiro; ele entra só "
            "DEPOIS que o cidadão submeter o Flow (inbound com _source='whatsapp_flow'). "
            "Encerre o turno logo após a tool call — não escreva texto depois (o body da "
            "tool já é a mensagem entregue; texto extra faz o interativo ser descartado). "
            "Parâmetros obrigatórios: `service_type` (ex: 'divida_ativa', 'reparo_luminaria') — "
            "o sistema resolve o flow_id automaticamente. NÃO passe `flow_id` nem `flow_token`. "
            "`body` (texto de introdução). Exemplos: "
            "reparo_luminaria: body='Vou abrir o formulário de defeito de luminária. Esse serviço não "
            "cobre falta de energia, luzes de casas ou semáforos apagados (acione Light 0800 0210196).' "
            "divida_ativa: body='Vou abrir o formulário de consulta de dívida ativa. Escolha o tipo de consulta "
            "e informe os dados solicitados.' "
            "PRÉ-PREENCHIMENTO (reparo_luminaria): passe `prefill_data` com os campos que o "
            "cidadão JÁ mencionou (ex: {'defect_type':'Apagada','location':'Rua','qty_pattern':'uma'}). "
            "NUNCA ponha PII (CPF/endereço) no prefill. Para divida_ativa, NÃO envie prefill. "
            "Opcional: `cta` (rótulo do botão, default 'Abrir formulário'), `header`/`footer`, "
            "`flow_action_payload` (tela inicial + data)."
        ),
    )
    def build_whatsapp_flow_envelope(
        body: str,
        service_type: str,
        flow_id: Optional[str] = None,
        flow_token: Optional[str] = None,
        cta: str = "Abrir formulário",
        header: Optional[str] = None,
        footer: Optional[str] = None,
        flow_action: str = "navigate",
        flow_action_payload: Optional[dict] = None,
        prefill_data: Optional[dict] = None,
    ) -> dict:
        import uuid

        from src.tools.whatsapp_interactive import (
            build_flow_envelope,
            encode_prefill_token,
        )

        # Resolve flow_id pelo service_type se não vier explícito.
        # O agente passa apenas service_type — nunca precisa saber o id.
        if not flow_id:
            flow_id = FLOW_TEMPLATES.get(service_type)
            if not flow_id:
                available = ", ".join(FLOW_TEMPLATES.keys())
                return {
                    "error": f"service_type '{service_type}' não encontrado. Disponíveis: {available}"
                }

        # flow_token gerado PELO MCP (2026-06-03): pedir pro modelo "gerar um UUID"
        # fazia o Gemini 2.5 Flash emitir `import uuid; uuid.uuid4()` como
        # function-call malformada (finish_reason=MALFORMED_FUNCTION_CALL) → turno
        # VAZIO, sem assistant_message, sem Flow → cidadão sem resposta. Tirando o
        # fardo do modelo (param opcional + geração aqui) some o gatilho. Aceita um
        # token vindo do modelo por back-compat, mas o normal é não vir.
        if not flow_token:
            flow_token = str(uuid.uuid4())

        # `encode_prefill_token` exige `service_type` pra normalizar/whitelist.
        if prefill_data and not service_type:
            service_type = next(
                (s for s, fid in FLOW_TEMPLATES.items() if fid == flow_id), None
            )

        # Pré-preenchimento: encoda os valores extraídos da conversa no flow_token
        # (canal pros data_exchange on-select, lido por _handle_init/_preserved_prefills).
        flow_token = encode_prefill_token(flow_token, prefill_data, service_type)

        # CRÍTICO (2026-06-03): com flow_action="navigate" o Meta NÃO chama o
        # endpoint _handle_init — ele abre a tela usando o `flow_action_payload.data`
        # INLINE. Então o token v1 sozinho deixa o form VAZIO (bug confirmado nos
        # logs do engine: token v1 correto, form vazio). Computamos aqui o MESMO
        # `data` que _handle_init devolveria (valores *_prefill + visibilidade
        # smart show_*) a partir do token e injetamos inline. O whatsapp_flow_sender
        # já injetava data inline (só os *_prefill); aqui vamos além incluindo os
        # show_* (senão campos prefillados podiam ficar escondidos). Token segue no envelope.
        if (
            service_type == "reparo_luminaria"
            and flow_action == "navigate"
            and isinstance(flow_token, str)
            and flow_token.startswith("v1:")
            and not flow_action_payload
        ):
            from src.flows.reparo_luminaria.handler import _handle_init

            _init = _handle_init(flow_token=flow_token)
            # O Flow PUBLICADO no Meta declara no data model do MAIN só
            # {defect_type_prefill, qty_pattern_prefill, location_prefill,
            # show_qty_pattern, show_location}. `show_quadra_question` existe só no
            # flow.json LOCAL (drift local↔Meta). O Meta valida o
            # flow_action_payload.data ESTRITO contra o schema da tela — uma chave
            # não-declarada faz ele REJEITAR o data inteiro → form vazio. Filtramos
            # pras chaves do schema publicado. (Reconciliar de vez = republicar o
            # Flow no Meta a partir do flow.json local.)
            _PUBLISHED_MAIN_KEYS = {
                "defect_type_prefill",
                "qty_pattern_prefill",
                "location_prefill",
                "show_qty_pattern",
                "show_location",
            }
            _data = {
                k: v
                for k, v in (_init.get("data") or {}).items()
                if k in _PUBLISHED_MAIN_KEYS
            }
            flow_action_payload = {"screen": _init.get("screen", "MAIN"), "data": _data}

        # CRÍTICO: se flow_action_payload ainda é None, usar a tela inicial
        # correta para o serviço. O Meta rejeita com 400 BAD_REQUEST se a
        # screen não existir no flow publicado. Cada serviço declara sua
        # `initial_screen` em FLOW_CONFIG (ex: "MAIN" para luminária,
        # "TIPO_CONSULTA" para dívida ativa). Sem isso, o default "MAIN" de
        # build_flow_envelope seria usado para todos os serviços, causando
        # rejeição silenciosa para flows que não têm tela "MAIN".
        if not flow_action_payload and flow_action == "navigate":
            _initial_screen = FLOW_CONFIG.get(service_type, {}).get(
                "initial_screen", "MAIN"
            )
            flow_action_payload = {"screen": _initial_screen}

        # Observabilidade: só as CHAVES (sem valores, pra não logar PII) + se o
        # token virou `v1:` + se o data inline foi montado. Diagnostica form vazio.
        logger.info(
            "build_whatsapp_flow_envelope flow_id={} service_type={} "
            "prefill_keys={} encoded_prefill={} inline_data_keys={}",
            flow_id,
            service_type,
            sorted((prefill_data or {}).keys()),
            isinstance(flow_token, str) and flow_token.startswith("v1:"),
            sorted((flow_action_payload or {}).get("data", {}).keys()),
        )

        return build_flow_envelope(
            flow_id=flow_id,
            body=body,
            flow_token=flow_token,
            cta=cta,
            header=header,
            footer=footer,
            flow_action=flow_action,
            flow_action_payload=flow_action_payload,
        )

    # WhatsApp interactive buttons (ADR-022): até 3 botões de resposta rápida.
    # Útil pra menu binário ("Sim/Não/Outro").
    @conditional_mcp_tool(
        "send_whatsapp_buttons",
        description=(
            "Envia botões de resposta rápida ao cidadão (até 3). Use quando "
            "o cidadão precisa escolher entre poucas opções discretas. "
            "Passe `body` (pergunta) e `buttons` lista de "
            "[{id: 'snake_case', title: 'Texto Visível'}]. Resposta do "
            "cidadão volta como interactive.button_reply.id no inbound."
        ),
    )
    def send_whatsapp_buttons(
        body: str,
        buttons: list,
        header: Optional[str] = None,
        footer: Optional[str] = None,
    ) -> dict:
        from src.tools.whatsapp_interactive import build_buttons_envelope

        return build_buttons_envelope(
            body=body, buttons=buttons, header=header, footer=footer
        )

    # WhatsApp interactive list (ADR-022): lista numerada com seções.
    # Até 10 rows totais (Meta limit). Útil pra "Escolha um serviço/bairro/etc".
    @conditional_mcp_tool(
        "send_whatsapp_list",
        description=(
            "Envia lista numerada ao cidadão (até 10 opções organizadas em "
            "seções). Use quando há mais de 3 opções (acima de 3, buttons "
            "lota a tela). Passe `body` (pergunta) e `sections` lista de "
            "[{title, rows: [{id, title, description?}]}]. Resposta do "
            "cidadão volta como interactive.list_reply.id no inbound."
        ),
    )
    def send_whatsapp_list(
        body: str,
        sections: list,
        button_label: str = "Ver opções",
        header: Optional[str] = None,
        footer: Optional[str] = None,
    ) -> dict:
        from src.tools.whatsapp_interactive import build_list_envelope

        return build_list_envelope(
            body=body,
            sections=sections,
            button_label=button_label,
            header=header,
            footer=footer,
        )

    @conditional_mcp_tool(
        "check_message_read_status",
        description=(
            "Verifica se uma mensagem do WhatsApp foi lida pelo cidadão (duplo "
            "check azul). Recebe o message_id retornado ao enviar a mensagem e "
            "consulta o status no Redis (populado via webhook). Retorna se foi "
            "lida, entregue ou enviada. Status disponível por até 7 dias após envio."
        ),
    )
    def whatsapp_check_message_read_status(message_id: str) -> dict:
        return check_message_read_status(message_id)

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

    @mcp.custom_route("/whatsapp-flow/luminaria", methods=["POST"])
    async def wa_flow_luminaria(request: Request):
        """
        Endpoint do WhatsApp Flow de coleta de defeito de luminária pública.
        Decripta o payload (RSA + AES-GCM), processa a action e devolve
        a resposta criptografada como bytes (exigido pelo protocolo Meta).
        """
        from fastapi.responses import Response

        private_key = env.WA_FLOWS_PRIVATE_KEY
        if not private_key:
            logger.error("wa_flow_luminaria: WA_FLOWS_PRIVATE_KEY não configurada")
            return JSONResponse(
                content={"error": "Flow não configurado"}, status_code=503
            )

        try:
            body = await request.json()
            encrypted_response = await process_flow_request(body, private_key)
            return Response(content=encrypted_response, media_type="text/plain")
        except ValueError as e:
            logger.error(f"wa_flow_luminaria: erro de decriptação: {e}")
            return JSONResponse(content={"error": str(e)}, status_code=421)
        except Exception as e:
            logger.error(f"wa_flow_luminaria: erro inesperado: {e}")
            return JSONResponse(content={"error": "Erro interno"}, status_code=500)

    @mcp.custom_route("/whatsapp-flow/divida-ativa", methods=["POST"])
    async def wa_flow_divida_ativa(request: Request):
        """
        Endpoint do WhatsApp Flow de consulta fiscal (Dívida Ativa).
        Decripta o payload (RSA + AES-GCM), processa a action e devolve
        a resposta criptografada como bytes (exigido pelo protocolo Meta).
        """
        from fastapi.responses import Response

        private_key = env.WA_FLOWS_PRIVATE_KEY
        if not private_key:
            logger.error("wa_flow_divida_ativa: WA_FLOWS_PRIVATE_KEY não configurada")
            return JSONResponse(
                content={"error": "Flow não configurado"}, status_code=503
            )

        try:
            body = await request.json()
            encrypted_response = await process_divida_ativa_flow_request(
                body, private_key
            )
            return Response(content=encrypted_response, media_type="text/plain")
        except ValueError as e:
            logger.error(f"wa_flow_divida_ativa: erro de decriptação: {e}")
            return JSONResponse(content={"error": str(e)}, status_code=421)
        except Exception as e:
            logger.error(f"wa_flow_divida_ativa: erro inesperado: {e}")
            return JSONResponse(content={"error": "Erro interno"}, status_code=500)

    @mcp.custom_route("/whatsapp-flow/divida-ativa-opcoes", methods=["POST"])
    async def wa_flow_divida_ativa_opcoes(request: Request):
        """
        Endpoint do WhatsApp Flow de opções de dívida ativa.
        Recebe INIT com flow_token contendo tem_nao_parcelado e tem_parcelado
        e retorna o array de opções filtrado dinamicamente.
        """
        from fastapi.responses import Response

        private_key = env.WA_FLOWS_PRIVATE_KEY
        if not private_key:
            logger.error(
                "wa_flow_divida_ativa_opcoes: WA_FLOWS_PRIVATE_KEY não configurada"
            )
            return JSONResponse(
                content={"error": "Flow não configurado"}, status_code=503
            )

        try:
            body = await request.json()
            encrypted_response = await process_divida_ativa_opcoes_flow_request(
                body, private_key
            )
            return Response(content=encrypted_response, media_type="text/plain")
        except ValueError as e:
            logger.error(f"wa_flow_divida_ativa_opcoes: erro de decriptação: {e}")
            return JSONResponse(content={"error": str(e)}, status_code=421)
        except Exception as e:
            logger.error(f"wa_flow_divida_ativa_opcoes: erro inesperado: {e}")
            return JSONResponse(content={"error": "Erro interno"}, status_code=500)

    @mcp.custom_route("/meta/webhook/status", methods=["POST"])
    async def handle_message_status_webhook(request: Request):
        """
        Webhook de status de mensagens WhatsApp (sent/delivered/read).

        Recebe notificações do Meta quando o status de uma mensagem muda.
        Armazena no Redis pra consulta posterior (ex: verificar se foi lida).

        Schema Meta:
          {
            "entry": [{
              "changes": [{
                "value": {
                  "statuses": [{
                    "id": "wamid.xxx",
                    "status": "sent|delivered|read|failed",
                    "timestamp": "1234567890",
                    "recipient_id": "5521999999999"
                  }]
                }
              }]
            }]
          }

        Docs: https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/components#statuses-object
        """
        try:
            body = await request.json()

            for entry in body.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})

                    for status in value.get("statuses", []):
                        message_id = status.get("id")
                        status_type = status.get("status")
                        timestamp = status.get("timestamp")
                        recipient_id = status.get("recipient_id", "")

                        if not message_id or not status_type:
                            logger.warning(
                                "webhook_status_incomplete",
                                status_obj=status,
                            )
                            continue

                        from src.utils.redis_client import get_redis_client

                        redis = get_redis_client()
                        key = f"msg_status:{message_id}"

                        redis.hset(
                            key,
                            mapping={
                                "status": status_type,
                                "timestamp": timestamp or "",
                                "recipient_id": recipient_id,
                                "updated_at": str(int(time.time())),
                            },
                        )
                        redis.expire(key, 7 * 24 * 60 * 60)

                        logger.info(
                            f"message_status_received: wamid={message_id} status={status_type} recipient={recipient_id}"
                        )

            return JSONResponse(content={"status": "ok"})

        except Exception as e:
            logger.error(f"webhook_status_error: {e}", exc_info=True)
            return JSONResponse(
                content={"status": "error", "message": str(e)},
                status_code=500,
            )

    # ===== LOG DE INICIALIZAÇÃO =====

    logger.info("Servidor FastMCP configurado com sucesso!")

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
