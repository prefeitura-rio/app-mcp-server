"""
Workflow de Poda de Árvore

Implementa o fluxo de solicitação de poda com verificação de cadastro.
"""

from loguru import logger
from langgraph.graph import StateGraph, END

from src.config.env import PODA_SERVICE_ID
from src.tools.multi_step_service.core.base_workflow import BaseWorkflow, handle_errors
from src.tools.multi_step_service.core.models import ServiceState, AgentResponse
from src.tools.multi_step_service.workflows.sgrc_components import CommonWorkflowConfig
from src.tools.multi_step_service.workflows.sgrc_components.address import (
    AddressFlowMixin,
)
from src.tools.multi_step_service.workflows.sgrc_components.identification import (
    IdentificationFlowMixin,
)
from src.tools.multi_step_service.workflows.sgrc_components.sgrc import SGRCTicketMixin

from src.tools.multi_step_service.workflows.poda_de_arvore import templates as tpl
from src.tools.multi_step_service.workflows.poda_de_arvore.integrations import (
    build_ticket_payload,
)
from src.tools.multi_step_service.workflows.sgrc_components.formatters import (
    mask_cpf,
    mask_email,
    mask_phone,
)
from src.tools.multi_step_service.workflows.sgrc_components.models import (
    CPFPayload,
    EmailPayload,
    NomePayload,
    AddressPayload,
    PontoReferenciaPayload,
    TicketDataConfirmationPayload,
)
from src.tools.multi_step_service.workflows.poda_de_arvore.api.api_service import (
    SGRCAPIService,
    AddressAPIService,
)

from src.utils.typesense_api import HubSearchRequest, hub_search_by_id


class PodaDeArvoreWorkflow(
    AddressFlowMixin,
    IdentificationFlowMixin,
    SGRCTicketMixin,
    BaseWorkflow,
):
    """
    Workflow de Poda de Árvore.

    Fluxo completo:
    1. Coleta endereço
    2. Confirmação do endereco
    3. Coleta ponto de referência
    4. Coleta CPF
    5. Verifica cadastro na API
    6. Coleta email
    7. Coleta nome
    8. Confirma todos os dados com o usuário
    9. Abre chamado na API do SGRC
    """

    service_name = "poda_de_arvore"
    description = "Solicitação de poda de árvore com verificação de cadastro."
    automatic_resets = True
    templates = tpl
    common_config = CommonWorkflowConfig(
        address_required=True,
        reference_point_required=False,
        identification_required=False,
    )

    steps_order = [
        "initialize",
        "collect_address",
        "collect_reference_point",
        "select_identification_method",
        "authenticate_govbr",
        "collect_cpf",
        "collect_email",
        "collect_name",
        "confirm_ticket_data",
        "open_ticket",
    ]

    step_dependencies = {
        "initialize": [],
        "collect_address": [],
        "collect_reference_point": ["collect_address"],
        "select_identification_method": ["collect_reference_point"],
        "authenticate_govbr": ["select_identification_method"],
        "collect_cpf": [
            "select_identification_method",
            "collect_email",
            "collect_name",
        ],
        "collect_email": [],
        "collect_name": [],
        "confirm_ticket_data": ["collect_address"],
        "open_ticket": ["collect_address", "confirm_ticket_data"],
    }

    def __init__(self, use_fake_api: bool = False):
        super().__init__()
        self.use_fake_api = use_fake_api
        self.service_id = "1614"
        self.service_knowledge = {}  # Armazena conhecimento do Typesense

        if not use_fake_api:
            self.api_service = SGRCAPIService()
            self.address_service = AddressAPIService()

    def build_ticket_payload(self, state: ServiceState):
        return build_ticket_payload(state)

    async def _load_service_knowledge(self) -> None:
        """Carrega conhecimento sobre o serviço de poda do Typesense."""
        try:
            request = HubSearchRequest(id=PODA_SERVICE_ID)
            result = await hub_search_by_id(request)

            if result and result.get("id"):
                self.service_knowledge = result

                logger.info("[KNOWLEDGE] Conhecimento carregado sobre poda de árvore")
                logger.debug(f"[KNOWLEDGE] Dados: {self.service_knowledge}")
            else:
                logger.warning(
                    "[KNOWLEDGE] Não foi possível carregar conhecimento do Typesense"
                )

        except Exception as e:
            logger.error(f"[KNOWLEDGE] Erro ao buscar conhecimento: {e}")
            # Continua sem o conhecimento adicional

    @handle_errors
    async def _initialize_workflow(self, state: ServiceState) -> ServiceState:
        """Inicializa o workflow e carrega conhecimento do serviço."""
        logger.info("[ENTRADA] _initialize_workflow")

        # COMENTADO: Funcionalidade de apresentar o serviço
        # # Se já apresentou o serviço, não apresenta novamente
        # if state.data.get("service_presented"):
        #     state.agent_response = None
        #     return state

        if not state.data.get("knowledge_loaded") and not self.service_knowledge:
            await self._load_service_knowledge()
            state.data["knowledge_loaded"] = True
            logger.info("[INITIALIZE] Conhecimento do serviço carregado")

        # Passa conhecimento para o state mas apenas informações essenciais
        if self.service_knowledge:
            # Passa apenas informações básicas que o agente pode precisar para responder perguntas
            state.data["service_info"] = {
                "nome": self.service_knowledge.get("title"),
                "resumo": self.service_knowledge.get("resumo"),
                "prazo": self.service_knowledge.get("tempo_atendimento"),
                "custo": self.service_knowledge.get("custo_servico"),
            }

        # COMENTADO: Instrução para apresentar o serviço
        # # Instrui o agente a apresentar o serviço e perguntar se deseja prosseguir
        # state.agent_response = AgentResponse(
        #     description="Apresentar brevemente (máximo 3 linhas): o que é o serviço e o prazo. Perguntar se o usuário deseja abrir uma solicitação.",
        #     payload_schema=AddressConfirmationPayload.model_json_schema()  # Usa schema de confirmação
        # )
        # state.data["service_presented"] = True

        # ORIGINAL: Retorna sem agent_response para seguir direto para collect_address
        state.agent_response = None
        return state

    @handle_errors
    async def _confirm_ticket_data(self, state: ServiceState) -> ServiceState:
        """Solicita confirmação dos dados antes de abrir o ticket."""
        logger.info("[ENTRADA] _confirm_ticket_data")

        # Se já confirmou, segue adiante
        if state.data.get("ticket_data_confirmed") is True:
            return state

        # Se tem payload com confirmação ou correção
        if "confirmacao" in state.payload or "correcao" in state.payload:
            try:
                validated = TicketDataConfirmationPayload.model_validate(state.payload)

                if validated.confirmacao is True:
                    state.data["ticket_data_confirmed"] = True
                    logger.info("Dados do ticket confirmados pelo usuário")
                    state.agent_response = None
                    return state

                elif validated.confirmacao is False or validated.correcao:
                    # Usuário quer corrigir algo
                    correcao_text = validated.correcao or ""
                    logger.info(f"Usuário solicitou correção: {correcao_text}")

                    if not correcao_text:
                        logger.info(
                            "Usuário disse 'não' sem especificar o que corrigir"
                        )
                        state.agent_response = AgentResponse(
                            description=tpl.solicitar_correcao_dados(),
                            payload_schema=TicketDataConfirmationPayload.model_json_schema(),
                        )
                        return state

                    # Analisa o que o usuário quer corrigir
                    correcao_lower = correcao_text.lower()

                    # Identifica qual campo precisa ser corrigido
                    if any(
                        word in correcao_lower
                        for word in [
                            "endereço",
                            "endereco",
                            "rua",
                            "avenida",
                            "praça",
                            "local",
                        ]
                    ):
                        # Volta para coletar endereço novamente
                        state.data["correction_requested"] = "address"
                        state.data.pop("address_confirmed", None)
                        state.data.pop("address_validated", None)
                        state.data.pop("address", None)
                        state.data.pop("ticket_data_confirmed", None)
                        # Reseta o contador de tentativas de endereço quando vem de correção
                        state.data.pop("address_validation", None)
                        state.agent_response = AgentResponse(
                            description=tpl.dados_corrigidos_solicitar_campo(
                                "endereco"
                            ),
                            payload_schema=AddressPayload.model_json_schema(),
                        )
                        return state

                    elif "nome" in correcao_lower:
                        # Volta para coletar nome
                        state.data["correction_requested"] = "name"
                        state.data.pop("name", None)
                        state.data.pop("name_processed", None)
                        state.data.pop("ticket_data_confirmed", None)
                        state.agent_response = AgentResponse(
                            description=tpl.dados_corrigidos_solicitar_campo("nome"),
                            payload_schema=NomePayload.model_json_schema(),
                        )
                        return state

                    elif "cpf" in correcao_lower:
                        # Volta para coletar CPF
                        state.data["correction_requested"] = "cpf"
                        state.data.pop("cpf", None)
                        state.data.pop("cadastro_verificado", None)
                        state.data["ticket_data_confirmed"] = (
                            False  # Marca que veio de correção
                        )
                        state.agent_response = AgentResponse(
                            description=tpl.dados_corrigidos_solicitar_campo("cpf"),
                            payload_schema=CPFPayload.model_json_schema(),
                        )
                        return state

                    elif "email" in correcao_lower or "e-mail" in correcao_lower:
                        # Volta para coletar email
                        state.data["correction_requested"] = "email"
                        state.data.pop("email", None)
                        state.data.pop("email_processed", None)
                        state.data.pop("ticket_data_confirmed", None)
                        state.agent_response = AgentResponse(
                            description=tpl.dados_corrigidos_solicitar_campo("email"),
                            payload_schema=EmailPayload.model_json_schema(),
                        )
                        return state

                    elif any(
                        word in correcao_lower
                        for word in ["ponto", "referência", "referencia"]
                    ):
                        # Volta para coletar ponto de referência
                        state.data["correction_requested"] = "reference_point"
                        state.data.pop("ponto_referencia", None)
                        state.data.pop("reference_point_collected", None)
                        state.data.pop("ticket_data_confirmed", None)
                        state.agent_response = AgentResponse(
                            description=tpl.dados_corrigidos_solicitar_campo(
                                "ponto_referencia"
                            ),
                            payload_schema=PontoReferenciaPayload.model_json_schema(),
                        )
                        return state
                    else:
                        # Não conseguiu identificar o que corrigir, pede mais detalhes
                        state.agent_response = AgentResponse(
                            description=tpl.solicitar_correcao_dados(),
                            payload_schema=TicketDataConfirmationPayload.model_json_schema(),
                        )
                        return state

            except Exception as e:
                logger.error(f"Erro ao processar confirmação de dados do ticket: {e}")
                state.agent_response = AgentResponse(
                    description=tpl.confirmar_resposta_invalida(),
                    payload_schema=TicketDataConfirmationPayload.model_json_schema(),
                    error_message=f"Resposta inválida: {str(e)}",
                )
                return state

        # Formata os dados para confirmação
        dados = []

        # Endereço
        if state.data.get("address"):
            address = state.data["address"]
            dados.append("📍 **ENDEREÇO:**")
            dados.append(self.format_address_confirmation(address))

        # Ponto de referência
        if state.data.get("ponto_referencia"):
            dados.append(
                f"\n📌 **PONTO DE REFERÊNCIA:**\n{state.data['ponto_referencia']}"
            )

        # Dados pessoais
        dados_pessoais = []
        if state.data.get("name"):
            dados_pessoais.append(f"- Nome: {state.data['name']}")
        if state.data.get("cpf"):  # Se CPF não está vazio
            cpf_mascarado = mask_cpf(state.data["cpf"])
            dados_pessoais.append(f"- CPF: {cpf_mascarado}")
        if state.data.get("email"):
            email_mascarado = mask_email(state.data["email"])
            dados_pessoais.append(f"- Email: {email_mascarado}")
        if state.data.get("phone"):
            telefone_mascarado = mask_phone(state.data["phone"])
            dados_pessoais.append(f"- Telefone: {telefone_mascarado}")

        if dados_pessoais:
            dados.append("\n👤 **DADOS DO SOLICITANTE:**")
            dados.extend(dados_pessoais)

        # Tipo de serviço
        dados.append("\n🌳 **SERVIÇO:** Poda de Árvore")

        dados_formatados = "\n".join(dados)

        state.agent_response = AgentResponse(
            description=tpl.confirmar_dados_ticket(dados_formatados),
            payload_schema=TicketDataConfirmationPayload.model_json_schema(),
        )

        return state

    # --- Roteamento Condicional ---

    def _route_after_address(self, state: ServiceState) -> str:
        logger.info("[ROTEAMENTO] _route_after_address")
        logger.info(
            f"[STATE.DATA] address_max_attempts_reached: {state.data.get('address_max_attempts_reached')}"
        )
        logger.info(
            f"[STATE.DATA] address_needs_confirmation: {state.data.get('address_needs_confirmation')}"
        )
        logger.info(
            f"[STATE.DATA] address_confirmed: {state.data.get('address_confirmed')}"
        )
        logger.info(
            f"[STATE.DATA] address_validated: {state.data.get('address_validated')}"
        )
        logger.info(f"[STATE] agent_response: {state.agent_response}")

        if state.agent_response:
            if not (
                state.data.get("address_validated")
                and state.data.get("address_confirmed")
            ):
                return END

        if state.data.get("address_needs_confirmation"):
            return "confirm_address"

        if state.data.get("address_validated") and state.data.get("address_confirmed"):
            # Se voltou de uma correção e já tem todos os outros dados, vai para confirmação
            if (
                state.data.get("reference_point_collected")
                and state.data.get("cpf")
                and state.data.get("email_processed")
                and state.data.get("name_processed")
            ):
                return "confirm_ticket_data"
            return "collect_reference_point"

        return "collect_address"

    def _route_after_confirmation(self, state: ServiceState) -> str:
        """Roteamento após confirmação de endereço."""
        logger.info("[ROTEAMENTO] _route_after_confirmation")

        if state.data.get("address_max_attempts_reached"):
            return END

        if state.agent_response:
            return END

        if state.data.get("address_confirmed"):
            # Se voltou de uma correção de endereço, volta para confirmação de dados
            if state.data.get("correction_requested") == "address":
                state.data.pop("correction_requested", None)
                return "confirm_ticket_data"
            # Se voltou de uma correção e já tem todos os outros dados, vai para confirmação
            if (
                state.data.get("reference_point_collected")
                and state.data.get("cpf")
                and state.data.get("email_processed")
                and state.data.get("name_processed")
            ):
                return "confirm_ticket_data"
            return "collect_reference_point"

        return "collect_address"

    def _route_after_reference(self, state: ServiceState) -> str:
        logger.info("[ROTEAMENTO] _route_after_reference")

        if state.agent_response:
            return END

        return "select_identification_method"

    def _route_after_method_selection(self, state: ServiceState) -> str:
        logger.info("[ROTEAMENTO] _route_after_method_selection")

        if state.agent_response:
            return END

        method = state.data.get("identification_method")
        logger.info(f"[ROUTE] Selected method: {method}")

        if method == "govbr":
            return "authenticate_govbr"
        else:
            return "collect_cpf"

    def _route_after_govbr_auth(self, state: ServiceState) -> str:
        logger.info("[ROTEAMENTO] _route_after_govbr_auth")

        if state.data.get("govbr_authenticated"):
            logger.info("[ROUTE] Gov.br auth completed")

            if not state.data.get("email"):
                logger.info("[ROUTE] Missing email, collecting")
                return "collect_email"
            if not state.data.get("name"):
                logger.info("[ROUTE] Missing name, collecting")
                return "collect_name"

            logger.info("[ROUTE] All data collected, going to confirmation")
            return "confirm_ticket_data"

        if state.agent_response:
            logger.info("[ROUTE] Waiting for user response")
            return END

        logger.info("[ROUTE] Falling back to CPF collection")
        return "collect_cpf"

    def _route_after_cpf(self, state: ServiceState) -> str:
        logger.info("[ROTEAMENTO] _route_after_cpf")
        logger.info(f"[STATE.DATA] cpf: {state.data.get('cpf')}")
        logger.info(
            f"[STATE.DATA] cadastro_verificado: {state.data.get('cadastro_verificado')}"
        )
        logger.info(
            f"[STATE.DATA] cpf_max_attempts_reached: {state.data.get('cpf_max_attempts_reached')}"
        )

        if state.data.get("cpf_max_attempts_reached"):
            state.agent_response = None
            return "collect_email"

        if state.agent_response:
            return END

        if state.data.get("awaiting_user_memory_confirmation"):
            return END

        # Se voltou de uma correção e já tem outros dados, vai direto para confirmação
        if state.data.get("email_processed") and state.data.get("name_processed"):
            return "confirm_ticket_data"

        if state.data.get("identificacao_pulada"):
            return "confirm_ticket_data"

        if state.data.get("cadastro_verificado"):
            if not state.data.get("email"):
                return "collect_email"
            if not state.data.get("name"):
                return "collect_name"
            return "confirm_ticket_data"

        if state.data.get("ticket_data_confirmed") is False:
            return "confirm_ticket_data"

        if not state.data.get("email_processed"):
            return "collect_email"
        if not state.data.get("name_processed"):
            return "collect_name"

        return "confirm_ticket_data"

    def _route_after_email(self, state: ServiceState) -> str:
        logger.info("[ROTEAMENTO] _route_after_email")
        logger.info(
            f"[STATE.DATA] email_processed: {state.data.get('email_processed')}"
        )
        logger.info(
            f"[STATE.DATA] email_max_attempts_reached: {state.data.get('email_max_attempts_reached')}"
        )

        if state.data.get("email_max_attempts_reached"):
            state.agent_response = None
            return "collect_name"

        if state.agent_response:
            return END

        if not state.data.get("email_processed"):
            return END

        # Se voltou de uma correção e já tem nome, vai direto para confirmação
        if state.data.get("name") or state.data.get("name_processed"):
            return "confirm_ticket_data"

        return "collect_name"

    def _route_after_name(self, state: ServiceState) -> str:
        logger.info("[ROTEAMENTO] _route_after_name")

        if state.data.get("name_max_attempts_reached"):
            state.agent_response = None
            return "confirm_ticket_data"

        if state.agent_response:
            return END

        if state.data.get("name_processed"):
            return "confirm_ticket_data"

        return END

    def _route_after_ticket_confirmation(self, state: ServiceState) -> str:
        """Roteamento após confirmação dos dados do ticket."""
        logger.info("[ROTEAMENTO] _route_after_ticket_confirmation")

        if state.data.get("ticket_data_confirmed") is True:
            # Usuário confirmou, criar o ticket
            return "open_ticket"

        # Se tem correção solicitada, roteia para o campo apropriado
        correction = state.data.get("correction_requested")
        if correction:
            state.data.pop("correction_requested", None)  # Limpa flag de correção

            if correction == "address":
                return "collect_address"
            elif correction == "name":
                return "collect_name"
            elif correction == "cpf":
                return "collect_cpf"
            elif correction == "email":
                return "collect_email"
            elif correction == "reference_point":
                return "collect_reference_point"

        # Aguardando confirmação ou correção
        return END

    def build_graph(self) -> StateGraph[ServiceState]:
        """
        Constrói o grafo do workflow de poda de árvore.

        Fluxo:
        1. Coleta endereço e confirma
        2. Coleta ponto de referência (opcional)
        3. Seleciona método de identificação (CPF ou Gov.br)
        4a. Se Gov.br: autentica via OAuth e extrai CPF/nome/email
        4b. Se CPF: coleta CPF manualmente e verifica cadastro
        5. Se não cadastrado ou faltando dados: coleta email e nome (opcionais)
        6. Confirma todos os dados com o usuário
        7. Abre chamado no SGRC
        """
        graph = StateGraph(ServiceState)

        # Adiciona os nós
        graph.add_node("initialize", self._initialize_workflow)
        graph.add_node("collect_address", self._collect_address)
        graph.add_node("confirm_address", self._confirm_address)
        graph.add_node("collect_reference_point", self._collect_reference_point)
        graph.add_node(
            "select_identification_method", self._select_identification_method
        )
        graph.add_node("authenticate_govbr", self._authenticate_govbr)
        graph.add_node("collect_cpf", self._collect_cpf)
        graph.add_node("collect_email", self._collect_email)
        graph.add_node("collect_name", self._collect_name)
        graph.add_node("confirm_ticket_data", self._confirm_ticket_data)
        graph.add_node("open_ticket", self._open_ticket)

        # Define o ponto de entrada
        graph.set_entry_point("initialize")

        # COMENTADO: Roteamento condicional para apresentação do serviço
        # # Fluxo: initialize -> END (para apresentar serviço) ou collect_address
        # graph.add_conditional_edges(
        #     "initialize",
        #     lambda state: END if state.agent_response else "collect_address",
        #     {
        #         "collect_address": "collect_address",
        #         END: END
        #     }
        # )

        # ORIGINAL: Edge direto do initialize para collect_address
        graph.add_edge("initialize", "collect_address")

        # Adiciona as rotas condicionais
        graph.add_conditional_edges(
            "collect_address",
            self._route_after_address,
            {
                "collect_address": "collect_address",
                "confirm_address": "confirm_address",
                "collect_reference_point": "collect_reference_point",
                "confirm_ticket_data": "confirm_ticket_data",
                END: END,
            },
        )

        graph.add_conditional_edges(
            "confirm_address",
            self._route_after_confirmation,
            {
                "collect_address": "collect_address",
                "collect_reference_point": "collect_reference_point",
                "confirm_ticket_data": "confirm_ticket_data",
                END: END,
            },
        )

        graph.add_conditional_edges(
            "collect_reference_point",
            self._route_after_reference,
            {
                "select_identification_method": "select_identification_method",
                "confirm_ticket_data": "confirm_ticket_data",
                END: END,
            },
        )

        graph.add_conditional_edges(
            "select_identification_method",
            self._route_after_method_selection,
            {
                "authenticate_govbr": "authenticate_govbr",
                "collect_cpf": "collect_cpf",
                END: END,
            },
        )

        graph.add_conditional_edges(
            "authenticate_govbr",
            self._route_after_govbr_auth,
            {
                "collect_cpf": "collect_cpf",
                "collect_email": "collect_email",
                "collect_name": "collect_name",
                "confirm_ticket_data": "confirm_ticket_data",
                END: END,
            },
        )

        graph.add_conditional_edges(
            "collect_cpf",
            self._route_after_cpf,
            {
                "collect_cpf": "collect_cpf",
                "collect_email": "collect_email",
                "collect_name": "collect_name",
                "confirm_ticket_data": "confirm_ticket_data",
                END: END,
            },
        )

        graph.add_conditional_edges(
            "collect_email",
            self._route_after_email,
            {
                "collect_email": "collect_email",
                "collect_name": "collect_name",
                "confirm_ticket_data": "confirm_ticket_data",
                END: END,
            },
        )

        graph.add_conditional_edges(
            "collect_name",
            self._route_after_name,
            {
                "collect_name": "collect_name",
                "confirm_ticket_data": "confirm_ticket_data",
                END: END,
            },
        )

        # Adiciona roteamento após confirmação de dados do ticket
        graph.add_conditional_edges(
            "confirm_ticket_data",
            self._route_after_ticket_confirmation,
            {
                "collect_address": "collect_address",
                "collect_reference_point": "collect_reference_point",
                "collect_cpf": "collect_cpf",
                "collect_email": "collect_email",
                "collect_name": "collect_name",
                "open_ticket": "open_ticket",
                END: END,
            },
        )

        # Após open_ticket, sempre termina (já tem agent_response definido)
        graph.add_edge("open_ticket", END)

        return graph
