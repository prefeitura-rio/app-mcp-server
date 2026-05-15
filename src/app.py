"""
Aplicação principal do servidor FastMCP para o Rio de Janeiro.
"""

# comment to trigger build

import os

from fastapi import Request
from fastapi.responses import PlainTextResponse, JSONResponse
from typing import Optional, List, Union

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
from src.tools.luminaria_flow import process_flow_request
from src.tools.whatsapp_flow_sender import send_flow_by_service, FLOW_TEMPLATES
from src.tools.divida_ativa import (
    emitir_guia_a_vista,
    emitir_guia_regularizacao,
    consultar_debitos,
)
from src.tools.langgraph_workflows import (
    multi_step_service as mss,
    tools_description as mss_tools_description,
    BACKEND_MODE,
)
from src.tools.multi_step_service.core.state import StateManager
from src.tools.multi_step_service.workflows.poda_de_arvore.api.api_service import (
    SGRCAPIService,
    AddressAPIService,
)
from src.tools.multi_step_service.workflows.sgrc_components.models import CPFPayload

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
        - `media_type` (obrig): 'image' | 'audio' | 'location' | 'unsupported'.
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
        Envia formulário interativo (WhatsApp Flow) para coleta estruturada de dados.

        QUANDO USAR:
        - Quando o cidadão solicitar um serviço que tem Flow disponível
        - ANTES de iniciar multi_step_service para o serviço
        - Apenas se o canal for WhatsApp (verificar contexto)

        FLOWS DISPONÍVEIS: {", ".join(FLOW_TEMPLATES.keys())}

        FLUXO:
        1. Detectar solicitação de serviço que tem Flow
        2. Chamar send_whatsapp_flow com o número do usuário e service_type
        3. Informar ao cidadão que o formulário foi enviado
        4. Aguardar resposta do flow (dados virão automaticamente via webhook)
        5. Workflow continuará com dados pré-preenchidos

        Args:
            user_number: Número do usuário no formato E.164 sem + (ex: 5521999999999)
            service_type: Tipo de serviço (ex: reparo_luminaria, poda_arvore)

        Returns:
            Confirmação de envio com message_id e instruções para o cidadão
        """,
    )
    async def send_whatsapp_flow(
        user_number: str,
        service_type: str,
    ) -> dict:
        """Envia WhatsApp Flow para coleta estruturada de dados."""
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
        )

        if result.get("success"):
            return {
                "success": True,
                "message": (
                    "Enviei um formulário rápido no WhatsApp para você preencher os dados. "
                    "Após preencher, eu continuo te ajudando com o restante."
                ),
                "flow_token": result.get("flow_token"),
                "next_step": "wait_for_flow_completion",
            }
        else:
            return {
                "success": False,
                "message": result.get("message", "Vamos continuar por texto."),
            }

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

            if source != "whatsapp_flow" and existing_state is None:
                # Só envia flow se não veio de flow completion E não há workflow ativo
                logger.info(
                    f"[AUTO_FLOW] Enviando WhatsApp Flow automaticamente para "
                    f"service={service_name}, user={user_id}"
                )
                flow_result = await send_flow_by_service(
                    service_type=service_name,
                    user_number=user_id,
                )

                if flow_result.get("success"):
                    return {
                        "status": "flow_sent",
                        "message": (
                            "Enviei um formulário rápido no WhatsApp para você preencher os dados. "
                            "Após preencher, eu continuo automaticamente com o atendimento."
                        ),
                        "flow_token": flow_result.get("flow_token"),
                        "next_step": "await_flow_completion",
                        "instruction": (
                            "IMPORTANTE: Informe ao cidadão que o formulário foi enviado. "
                            "NÃO prossiga coletando dados manualmente. Aguarde o webhook "
                            "com os dados preenchidos - o workflow será chamado automaticamente."
                        ),
                    }
                else:
                    # Flow falhou, continuar normalmente por texto
                    logger.warning(
                        f"[AUTO_FLOW] Falha ao enviar flow: {flow_result.get('error')}"
                    )
            elif existing_state is not None and source != "whatsapp_flow":
                logger.info(
                    f"[AUTO_FLOW] Workflow já ativo para service={service_name}, "
                    f"user={user_id} - NÃO enviando flow novamente"
                )

        # Prosseguir normalmente com o workflow
        response = await mss(
            service_name=service_name, user_id=user_id, payload=payload
        )
        return response

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
        """
        address_service = AddressAPIService()
        return await address_service.reverse_geolocator(latitude, longitude)

    @conditional_mcp_tool("get_user_by_cpf")
    async def get_user_by_cpf(cpf: str) -> dict:
        """
        Consulta cadastro do cidadão por CPF válido no sistema da Prefeitura do Rio.
        Use apenas quando o usuário informou explicitamente um CPF com 11 dígitos.
        Antes de chamar, remova pontuação e confirme que sobraram exatamente 11 dígitos.
        Não use para inscrição imobiliária, número de protocolo, guia ou outros identificadores.
        Retorna nome, e-mail e telefone se o cidadão estiver cadastrado.
        """
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
            locality=reference_point,
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

        private_key = env.WA_LUMINARIA_PRIVATE_KEY
        if not private_key:
            logger.error("wa_flow_luminaria: WA_LUMINARIA_PRIVATE_KEY não configurada")
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
