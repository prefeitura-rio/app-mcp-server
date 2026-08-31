"""
Aplicação principal do servidor FastMCP para o Rio de Janeiro.
"""

# comment to trigger build

import asyncio
import json

from contextlib import asynccontextmanager, suppress

from fastapi import Request
from fastapi.responses import JSONResponse
from typing import Annotated, Optional, List, Union

from pydantic import Field

from src.tools.web_search_surkai import surkai_search
from src.tools.dharma_search import dharma_search
from src.utils.log import logger
from src.config.settings import Settings
from src.middleware.hybrid_verifier import HybridTokenVerifier
from src.middleware.body_limit import LimitRequestBodyMiddleware
from src.middleware.require_auth import RequireAuthOnAllRoutes
from src.observability.tracing import ToolCallTracingMiddleware, setup_tracing
from src.health.checks import register_default_checks
from src.health.external_tables import run_probe_loop
from src.health.registry import health_registry
from src.health.routes import register_health_routes
from src.health.state import set_ready
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
from src.tools.divida_ativa import (
    emitir_guia_a_vista,
    emitir_guia_regularizacao,
    consultar_debitos,
)
from src.tools.divida_ativa_v2 import (
    emitir_guia_a_vista_v2,
    emitir_guia_regularizacao_v2,
)
from src.tools.langgraph_workflows import (
    multi_step_service as mss,
    tools_description as mss_tools_description,
)
from src.tools.rock_in_rio.cache import aquecer_lineup, run_refresh_loop
from src.tools.rock_in_rio.tool import (
    get_rock_in_rio_lineup,
    descricao_da_tool as rock_in_rio_description,
)

from src.resources.rio_info import (
    get_districts_list,
    get_rio_basic_info,
    get_greeting_message,
)

from src.config.env import IS_LOCAL, EXCLUDED_TOOLS
import src.config.env as env
from src.utils.tool_versioning import add_tool_version, get_tool_version_from_file

# Um único caminho de import, deliberadamente. Antes havia bifurcação — o
# `mcp.server.fastmcp` quando `IS_LOCAL`, o `fastmcp` caso contrário — e ela
# custava caro: o CI roda sempre com `IS_LOCAL` falso, então metade deste
# módulo nunca era exercitada, e a divergência só aparecia na máquina de quem
# estivesse desenvolvendo. O mcp 2.x fecha a questão de vez: `mcp.server.fastmcp`
# deixou de existir (a classe virou `MCPServer`), então o ramo antigo hoje é um
# ImportError esperando alguém rodar localmente.
from fastmcp import FastMCP

TOOL_VERSION = get_tool_version_from_file()["version"]

# Tetos de tamanho dos argumentos das tools (B-06).
#
# Nenhum dos 17 schemas declarava restrição, então `query`, `address` e
# `description` aceitavam string de tamanho arbitrário — e `google_search` e
# `dharma_search_tool` disparam Gemini a cada chamada. O teto não é validação de
# negócio: é o limite acima do qual a entrada deixa de ser plausível e vira
# custo. Por isso são folgados (ordens de grandeza acima do uso real): recusar
# entrada legítima seria pior do que o problema que resolvem.
#
# O FastMCP propaga isto para o JSON Schema da tool, então o próprio cliente MCP
# recusa a chamada malformada — antes de custar uma chamada de API paga.
LIMITE_BUSCA = 2000  # pergunta de cidadão em chat; 2000 já é um parágrafo longo
LIMITE_ENDERECO = 300  # logradouro + número + complemento + bairro
LIMITE_RELATO = 4000  # relato de ocorrência ditado por voz
LIMITE_FEEDBACK = 8000
# `payload_json` do `multi_step_service` carrega o payload inteiro de um passo de
# workflow — o maior deles é o `dicionario_itens` da Dívida Ativa, com centenas
# de CDAs, na casa das dezenas de KB. 256 KiB é uma ordem de grandeza acima
# disso. O teto que existia era o do transporte (`MAX_REQUEST_BODY_BYTES`, 1
# MiB): limita a memória do processo, mas não impede que a string chegue inteira
# ao `json.loads`, e não aparece no schema que o cliente MCP consulta.
LIMITE_PAYLOAD = 262144
LIMITE_ID = 64  # telefone em E.164 tem 15
LIMITE_NOME_CURTO = 128  # nome de memória, tema, tipo de alerta

# Preenchido por `create_app()`. Fica `None` em execução local, onde não há
# autenticação por decisão explícita (ver o bloco de auth em `create_app`).
_auth_provider = None


def create_app() -> FastMCP:
    """
    Cria e configura a aplicação FastMCP.

    Returns:
        Instância configurada do FastMCP
    """
    # Monta o provider de autenticação híbrido (JWT do Keycloak "Identidade
    # Carioca" + token estático legado) apenas em ambientes não-locais,
    # preservando o comportamento local atual de zero fricção (sem auth).
    global _auth_provider
    auth_provider = None
    if not IS_LOCAL:
        valid_tokens = env.VALID_TOKENS
        # Entradas vazias são descartadas: `"".split(",")` devolve `[""]`, o
        # que colocaria um token vazio no set de tokens aceitos.
        static_tokens = (
            [t.strip() for t in valid_tokens.split(",") if t.strip()]
            if isinstance(valid_tokens, str)
            else valid_tokens
        )
        keycloak_partially_configured = bool(env.KEYCLOAK_ISSUER) != bool(
            env.KEYCLOAK_JWKS_URI
        )
        if keycloak_partially_configured:
            logger.warning(
                "Configuração do Keycloak incompleta (KEYCLOAK_ISSUER=%r, "
                "KEYCLOAK_JWKS_URI=%r): autenticação via JWT permanecerá "
                "DESATIVADA até ambos estarem preenchidos — só o token "
                "estático (VALID_TOKENS) será aceito.",
                bool(env.KEYCLOAK_ISSUER),
                bool(env.KEYCLOAK_JWKS_URI),
            )
        elif env.KEYCLOAK_ISSUER and not env.KEYCLOAK_TRUSTED_CLIENTS:
            logger.warning(
                "KEYCLOAK_ISSUER configurado sem KEYCLOAK_TRUSTED_CLIENTS: "
                "qualquer client válido do realm será aceito."
            )
        auth_provider = HybridTokenVerifier(
            static_tokens=static_tokens,
            jwks_uri=env.KEYCLOAK_JWKS_URI,
            issuer=env.KEYCLOAK_ISSUER,
            allowed_azp=env.KEYCLOAK_TRUSTED_CLIENTS,
        )
    # Guardado em nível de módulo para que `build_http_middleware()` use
    # exatamente o mesmo verificador que o FastMCP usa em `/mcp` — sem uma
    # segunda noção de "token válido" para manter em sincronia.
    _auth_provider = auth_provider

    # Inicializa o servidor FastMCP
    # Observabilidade: habilita tracing OpenTelemetry (exportando para o
    # SigNoz) se OTEL_EXPORTER_OTLP_TRACES_ENDPOINT estiver configurado.
    # `setup_tracing()` é seguro mesmo sem configuração (retorna False).
    mcp_middleware = []
    if setup_tracing() and not IS_LOCAL:
        mcp_middleware.append(ToolCallTracingMiddleware())

    @asynccontextmanager
    async def health_lifespan(server):
        """Sonda as dependências uma vez no startup e drena no shutdown.

        A sondagem inicial é informativa: uma dependência fora do ar é logada
        e o servidor sobe assim mesmo, porque falha de conectividade é
        transitória e derrubar o pod por causa dela converteria degradação
        parcial em indisponibilidade total. O que impede o boot são erros de
        configuração, verificados antes disto por `run_startup_preflight()`.
        """
        # Catálogo de tools registradas. Mora aqui, e não em `create_app()`,
        # porque `list_tools()` é corrotina e a fábrica é síncrona. Antes isto
        # lia `mcp._tool_manager._tools` — API privada, que sumiu no fastmcp 4.
        # `list_tools()` é a pública, e como efeito colateral o catálogo passa a
        # ser logado no startup do servidor, que é quando ele de fato importa.
        try:
            nomes = sorted(tool.name for tool in await mcp.list_tools())
            logger.info(f"Tools registradas ({len(nomes)}): {nomes}")
        except Exception as e:
            logger.warning(f"Erro ao listar tools: {e}")

        try:
            results = await health_registry.run_all(force=True)
            for result in results:
                logger.info(
                    f"Health check '{result.name}': {result.status.value} "
                    f"({result.latency_ms}ms)"
                )
        except Exception:
            logger.exception("Falha ao sondar dependências no startup")

        set_ready(True)

        # Expiração dos arquivos de fallback da DLQ (CHATR-126). Roda aqui, e
        # não só dentro do worker de drain, porque o TTL desses arquivos é o que
        # limita a retenção do dado pessoal que vai no payload — e com o drain
        # desligado (`BIGQUERY_DLQ_DRAIN_ENABLED=false`) ou em execução local
        # nada os apagava. Aguardado de propósito: é uma varredura de diretório,
        # rápida, e falha sua não impede o boot (a função engole a exceção).
        from src.utils.bigquery import expirar_arquivos_dlq_async

        await expirar_arquivos_dlq_async()

        # Sonda das tabelas externas de Sheets (CHATR-119). Fica fora do
        # `health_registry` de propósito: exige query real (o `dry_run` do
        # `check_bigquery` não detecta "Spreadsheet not found") e custa
        # segundos, acima do teto da rodada de `/health/detail`. O check
        # homônimo lê o veredito que este laço produz. A referência é mantida
        # em variável local viva pelo escopo do lifespan — sem ela, o GC pode
        # coletar a task, já que a event loop guarda apenas referência fraca.
        probe_task = None
        if not IS_LOCAL:
            probe_task = asyncio.create_task(run_probe_loop())

        # Worker que devolve ao BigQuery as escritas que caíram na DLQ
        # (CHATR-126). Sem ele a DLQ só acumula: o payload deixa de se perder,
        # mas também nunca chega à tabela de destino. Mesma precaução de
        # referência viva do `probe_task` acima — a event loop guarda apenas
        # referência fraca para as tasks.
        dlq_drain_task = None
        if not IS_LOCAL and getattr(env, "BIGQUERY_DLQ_DRAIN_ENABLED", True):
            from src.utils.bigquery import drain_bigquery_dlq_loop

            dlq_drain_task = asyncio.create_task(drain_bigquery_dlq_loop())

        # Line-up do Rock in Rio (CHATR-187). O aquecimento é aguardado de
        # propósito: sem ele o primeiro cidadão a perguntar depois de cada
        # deploy pagaria o download dos sete dias dentro da própria conversa.
        # `aquecer_lineup` engole as próprias exceções, então uma fonte fora do
        # ar aqui custa o timeout (as sete páginas baixam concorrentes) e nada
        # mais.
        #
        # Atenção ao custo: o `set_ready(True)` acima é anterior, mas isso NÃO
        # livra a readiness. A uvicorn só cria o socket depois que o startup do
        # lifespan retorna, então enquanto este `await` roda a porta sequer
        # escuta e a probe leva connection refused. O atraso é limitado pelo
        # `TIMEOUT_S` do scraper; qualquer coisa mais cara que isso aqui precisa
        # virar task em vez de `await`.
        #
        # A exclusão da tool desliga junto o trabalho de fundo. Sem essa guarda,
        # `EXCLUDED_TOOLS` tirava a tool do catálogo e deixava o laço batendo no
        # site de 15 em 15 minutos, para sempre, por ninguém.
        #
        # Desligado em execução local pelo mesmo critério das tasks acima: em
        # `mcp dev` a primeira chamada carrega o cache sozinha, pelo caminho
        # preguiçoso de `obter_lineup`.
        lineup_task = None
        if not IS_LOCAL and "rock_in_rio_lineup" not in EXCLUDED_TOOLS:
            await aquecer_lineup()
            lineup_task = asyncio.create_task(run_refresh_loop())

        try:
            yield {}
        finally:
            for task in (probe_task, dlq_drain_task, lineup_task):
                if task is None:
                    continue
                task.cancel()
                # `cancel()` só agenda a interrupção; aguardar aqui garante
                # que o laço não sobreviva ao encerramento programático.
                with suppress(asyncio.CancelledError):
                    await task

            # Atenção: este bloco NÃO roda em SIGTERM. A uvicorn restaura o
            # handler original do sinal e faz `signal.raise_signal()` ao sair
            # de `serve()` (Server.capture_signals), então o processo morre
            # pelo handler padrão antes de o lifespan do FastMCP desenrolar.
            # Quem tira o pod do balanceador no encerramento é o próprio
            # Kubernetes, ao marcá-lo como Terminating. Mantido porque está
            # correto no encerramento programático (ex.: testes, embedding).
            #
            # Trabalho que PRECISA acontecer em SIGTERM não pode morar aqui.
            # O flush do buffer de escrita do BigQuery se encaixa nisso e usa
            # o outro caminho: `src/utils/bigquery.py` instala o próprio
            # handler antes de a uvicorn capturar os sinais, justamente para
            # ser o "handler anterior" que ela restaura e re-levanta.
            set_ready(False)
            logger.info("Shutdown iniciado: readiness marcada como indisponível.")

    mcp_kwargs = {
        "name": Settings.SERVER_NAME,
        "auth": auth_provider,
        "lifespan": health_lifespan,
        # "version": Settings.VERSION,
    }
    if mcp_middleware:
        mcp_kwargs["middleware"] = mcp_middleware

    mcp = FastMCP(**mcp_kwargs)  # pyright: ignore[reportCallIssue]

    def conditional_mcp_tool(tool_name: str, **kwargs):
        """Wrapper to conditionally register tools based on EXCLUDED_TOOLS"""

        def decorator(func):
            if tool_name not in EXCLUDED_TOOLS:
                return mcp.tool(**kwargs)(func)
            else:
                logger.info(f"Tool '{tool_name}' excluded from registration")
                return func

        return decorator

    # /health (liveness, trivial), /health/ready (readiness) e /health/detail
    # (diagnóstico das dependências) — ver src/health/routes.py.
    register_health_routes(mcp)
    register_default_checks(mcp)

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
    async def google_search(
        query: Annotated[str, Field(max_length=LIMITE_BUSCA)],
    ) -> dict:
        """Obtém os resultados da busca no Google"""
        response = await get_google_search(query)
        return response

    @conditional_mcp_tool("web_search_surkai")
    async def web_search_surkai(
        query: Annotated[str, Field(max_length=LIMITE_BUSCA)],
    ) -> dict:
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
    async def dharma_search_tool(
        query: Annotated[str, Field(max_length=LIMITE_BUSCA)],
    ) -> dict:
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
        address: Annotated[str, Field(max_length=LIMITE_ENDERECO)],
        categories: Annotated[Optional[List[str]], Field(max_length=40)] = None,
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
    async def equipments_instructions(
        tema: Annotated[str, Field(max_length=LIMITE_NOME_CURTO)] = "geral",
    ) -> dict:
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

    @conditional_mcp_tool(
        "rock_in_rio_lineup",
        description=rock_in_rio_description(TOOL_VERSION),
    )
    async def rock_in_rio_lineup() -> dict:
        # Sem parâmetros de propósito: a grade inteira cabe na resposta, e é a
        # LLM que faz o recorte pedido pelo cidadão. Ver `tool.py`.
        return add_tool_version(await get_rock_in_rio_lineup())

    @conditional_mcp_tool("get_user_memory")
    async def get_user_memory(
        user_id: Annotated[str, Field(max_length=LIMITE_ID)],
        memory_name: Annotated[
            Optional[Union[str, None]], Field(max_length=LIMITE_NOME_CURTO)
        ] = None,
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
    async def upsert_user_memory(
        user_id: Annotated[str, Field(max_length=LIMITE_ID)],
        memory_bank: dict,
    ) -> dict:
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
    async def user_feedback(
        user_id: Annotated[str, Field(max_length=LIMITE_ID)],
        feedback: Annotated[str, Field(max_length=LIMITE_FEEDBACK)],
    ) -> dict:
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
        user_id: Annotated[str, Field(max_length=LIMITE_ID)],
        # `alert_type` e `severity` NÃO viram `Literal`: `create_cor_alert` já
        # valida os dois e devolve `{"success": False, "error": ...}` com a
        # lista de valores aceitos, que é uma resposta que o agente sabe
        # aproveitar. Um `Literal` trocaria isso por erro de schema — mudaria o
        # contrato com o cliente MCP para proteger o que já está protegido.
        alert_type: Annotated[str, Field(max_length=LIMITE_NOME_CURTO)],
        severity: Annotated[str, Field(max_length=LIMITE_NOME_CURTO)],
        description: Annotated[str, Field(max_length=LIMITE_RELATO)],
        address: Annotated[str, Field(max_length=LIMITE_ENDERECO)],
    ) -> dict:
        response = await create_cor_alert(
            user_id=user_id,
            alert_type=alert_type,
            severity=severity,
            description=description,
            address=address,
        )
        return add_tool_version(response)

    @conditional_mcp_tool("multi_step_service", description=mss_tools_description)
    async def multi_step_service(
        service_name: Annotated[str, Field(max_length=LIMITE_NOME_CURTO)],
        user_id: Annotated[str, Field(max_length=LIMITE_ID)],
        payload_json: Annotated[str, Field(max_length=LIMITE_PAYLOAD)] = "{}",
    ) -> dict:
        try:
            payload = json.loads(payload_json or "{}")
        except json.JSONDecodeError:
            return {
                "service_name": service_name,
                "error_message": "payload_json deve ser um JSON object valido",
                "description": "",
                "payload_schema": None,
                "data": {},
            }

        if not isinstance(payload, dict):
            return {
                "service_name": service_name,
                "error_message": "payload_json deve ser um JSON object valido",
                "description": "",
                "payload_schema": None,
                "data": {},
            }

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

        DEPRECATED: use POST /v2/emitir_guia, que valida a entrada com
        Pydantic antes de chamar a PGM.
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

        DEPRECATED: use POST /v2/emitir_guia_regularizacao, que valida a
        entrada com Pydantic antes de chamar a PGM.
        """
        try:
            parameters = await request.json()
            result = await emitir_guia_regularizacao(parameters)
            return JSONResponse(content=result, status_code=200)
        except Exception as e:
            logger.error(f"Error processing request: {str(e)}")
            return JSONResponse(content={"error": str(e)}, status_code=500)

    # ===== DÍVIDA ATIVA V2 (entrada e saída validadas com Pydantic) =====
    # Erros de validação retornam HTTP 200 com api_resposta_sucesso=false,
    # mantendo o contrato que os consumidores (SFMC/LLM) já tratam.

    @mcp.custom_route("/v2/emitir_guia", methods=["POST"])
    async def da_emitir_guia_pagamento_a_vista_v2(request: Request) -> JSONResponse:
        """
        Endpoint para emitir guia de pagamento à vista, com validação de entrada
        """
        try:
            parameters = await request.json()
            result = await emitir_guia_a_vista_v2(parameters)
            return JSONResponse(content=result, status_code=200)
        except Exception as e:
            logger.error(f"Error processing request: {str(e)}")
            return JSONResponse(content={"error": str(e)}, status_code=500)

    @mcp.custom_route("/v2/emitir_guia_regularizacao", methods=["POST"])
    async def da_emitir_guia_regularizacao_v2(request: Request) -> JSONResponse:
        """
        Endpoint para emitir guia de regularização, com validação de entrada
        """
        try:
            parameters = await request.json()
            result = await emitir_guia_regularizacao_v2(parameters)
            return JSONResponse(content=result, status_code=200)
        except Exception as e:
            logger.error(f"Error processing request: {str(e)}")
            return JSONResponse(content={"error": str(e)}, status_code=500)

    # ===== LOG DE INICIALIZAÇÃO =====

    logger.info("Servidor FastMCP configurado com sucesso!")

    if Settings.DEBUG:
        logger.debug("Modo DEBUG ativado")
        logger.debug(f"Configurações: {Settings.get_server_info()}")

    # Fallback para modos de execução que não entram no lifespan: nada é
    # servido por HTTP antes de `create_app()` retornar, então marcar aqui não
    # abre janela para tráfego prematuro — e evita que `/health/ready` fique
    # preso em 503 caso o lifespan não seja acionado.
    set_ready(True)

    return mcp


def build_http_middleware(extra=None) -> list:
    """Middleware ASGI aplicado a TODAS as rotas HTTP, não só a `/mcp`.

    Precisa ser a mesma lista em todo caminho que monta a aplicação HTTP
    (`src/main.py` em produção, os testes em CI), senão a proteção existe só
    onde alguém lembrou de ligá-la. Por isso mora aqui, junto de quem constrói
    o servidor, e não no ponto de entrada.

    A ordem importa: o teto de corpo vem primeiro para que um POST gigante seja
    cortado antes de qualquer trabalho de verificação de token.

    Args:
        extra: middleware adicional (ex.: instrumentação OTel), aplicado por
            último — mais perto da rota.
    """
    from starlette.middleware import Middleware as StarletteMiddleware

    middleware = [
        StarletteMiddleware(
            LimitRequestBodyMiddleware,
            max_bytes=env.MAX_REQUEST_BODY_BYTES,
        )
    ]

    # Sem provider não há o que verificar: é o caso de `IS_LOCAL`, em que a
    # ausência de autenticação é deliberada. Ligar o middleware aqui recusaria
    # toda requisição local, já que nenhum token seria válido.
    if _auth_provider is not None:
        kwargs = {"verifier": _auth_provider, "mode": env.HTTP_AUTH_MODE}
        # Só sobrescreve a lista de legado quando o ambiente diz algo. Passar
        # `None` aqui apagaria o default do middleware e exigiria token nas
        # rotas antigas — que é justamente o que ainda não se quer.
        if env.HTTP_AUTH_OBSERVE_PATHS is not None:
            kwargs["observe_paths"] = env.HTTP_AUTH_OBSERVE_PATHS
        middleware.append(StarletteMiddleware(RequireAuthOnAllRoutes, **kwargs))

    if extra:
        middleware.extend(extra)
    return middleware


# Instância global da aplicação
mcp = create_app()

# Alias para retro-compatibilidade
app = mcp

# comment to trigger github actions
