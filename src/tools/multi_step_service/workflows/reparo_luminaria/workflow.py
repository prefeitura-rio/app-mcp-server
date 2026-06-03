import unicodedata
from typing import Any, Dict

from langgraph.graph import END, StateGraph
from loguru import logger

from src.tools.multi_step_service.core.base_workflow import BaseWorkflow, handle_errors
from src.tools.multi_step_service.core.models import AgentResponse, ServiceState
from src.tools.multi_step_service.workflows.reparo_luminaria import templates as tpl
from src.tools.multi_step_service.workflows.reparo_luminaria.api.api_service import (
    AddressAPIService,
    SGRCAPIService,
)
from src.tools.multi_step_service.workflows.reparo_luminaria.integrations import (
    build_ticket_payload,
)
from src.tools.multi_step_service.workflows.reparo_luminaria.models import (
    ConfirmacaoServicoPayload,
    LuminariaDefeitoPayload,
    LuminariaIntercaladasBlocoPayload,
    LuminariaLocalizacaoPayload,
    LuminariaQuantidadePayload,
    QuadraEsportesPayload,
)
from src.tools.multi_step_service.workflows.sgrc_components import CommonWorkflowConfig
from src.tools.multi_step_service.workflows.sgrc_components.address import (
    AddressFlowMixin,
)
from src.tools.multi_step_service.workflows.sgrc_components.identification import (
    IdentificationFlowMixin,
)
from src.tools.multi_step_service.workflows.sgrc_components.formatters import (
    mask_cpf,
    mask_email,
    mask_phone,
)
from src.tools.multi_step_service.workflows.sgrc_components.models import (
    AddressPayload,
    CPFPayload,
    EmailPayload,
    NomePayload,
    PontoReferenciaPayload,
    TicketDataConfirmationPayload,
)
from src.tools.multi_step_service.workflows.sgrc_components.sgrc import SGRCTicketMixin
from src.utils.typesense_api import HubSearchRequest, hub_search, hub_search_by_id


def _strip_accents(value: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", value or "")
        if not unicodedata.combining(char)
    ).lower()


class ReparoLuminariaWorkflow(
    AddressFlowMixin,
    IdentificationFlowMixin,
    SGRCTicketMixin,
    BaseWorkflow,
):
    """Workflow de Reparo de Luminária (RLU 18131)."""

    service_name = "reparo_luminaria"
    description = "Solicitação de reparo de luminária de iluminação pública."
    automatic_resets = True
    templates = tpl
    common_config = CommonWorkflowConfig(
        address_required=True,
        reference_point_required=False,
        identification_required=False,
    )

    steps_order = [
        "initialize",
        "collect_luminaria_details",
        "collect_address",
        "collect_quadra_esportes",
        "collect_reference_point",
        "collect_cpf",
        "collect_email",
        "collect_name",
        "confirm_ticket_data",
        "open_ticket",
    ]

    step_dependencies = {
        "initialize": [],
        "collect_luminaria_details": [],
        "collect_address": ["collect_luminaria_details"],
        "collect_quadra_esportes": ["collect_address"],
        "collect_reference_point": ["collect_address"],
        "collect_cpf": ["collect_address"],
        "collect_email": [],
        "collect_name": [],
        "confirm_ticket_data": ["collect_luminaria_details", "collect_address"],
        "open_ticket": ["confirm_ticket_data"],
    }

    def __init__(self, use_fake_api: bool = False):
        super().__init__()
        self.use_fake_api = use_fake_api
        self.service_id = "18131"
        self.knowledge_service_id = "46f2d094-030c-449f-8f9d-5437a7243a43"
        self.service_knowledge = {}

        if not use_fake_api:
            self.api_service = SGRCAPIService()
            self.address_service = AddressAPIService()

    def build_ticket_payload(self, state: ServiceState):
        return build_ticket_payload(state)

    def build_specific_attributes(self, state: ServiceState) -> Dict[str, Any]:
        localizacao_luminaria = state.data.get("luminaria_localizacao")
        localizacao_quadra = (
            "1" if localizacao_luminaria == "Quadra de esportes" else "0"
        )
        localizacao_praca = (
            "1"
            if self._logradouro_indicador_praca(state)
            or localizacao_luminaria == "Praça"
            else "0"
        )

        if localizacao_quadra == "1":
            localizacao_luminaria = "Quadra"

        return {
            "defeitoLuminaria": state.data.get("luminaria_defeito_classificado", ""),
            "dentroQuadraEsporte": localizacao_quadra,
            "estaNaPraca": localizacao_praca,
            "localizacaoLuminaria": localizacao_luminaria,
            "nomePraca": "",
        }

    def _classifica_defeito(self, state: ServiceState) -> None:
        mapping = {
            ("Apagada", "uma", None): "Apagada",
            ("Apagada", "grupo", "bloco"): "Bloco ou grupo de luminárias apagadas",
            (
                "Apagada",
                "grupo",
                "intercaladas",
            ): "Várias luminárias intercaladas apagadas",
            ("Piscando", "uma", None): "Piscando",
            ("Piscando", "grupo", "bloco"): "Bloco ou grupo de luminárias piscando",
            (
                "Piscando",
                "grupo",
                "intercaladas",
            ): "Bloco ou grupo de luminárias piscando",
            ("Acesa de dia", "uma", None): "Acesa durante o dia",
            (
                "Acesa de dia",
                "grupo",
                "bloco",
            ): "Bloco ou grupo de luminárias acesas de dia",
            (
                "Acesa de dia",
                "grupo",
                "intercaladas",
            ): "Várias luminárias intercaladas acesas de dia",
            ("Pendurada", None, None): "Pendurada",
            ("Danificada", None, None): "Danificada",
            ("Com ruído", None, None): "Com ruído",
        }
        # Defeitos non-visual (Pendurada/Danificada/Com ruído) só têm entrada
        # no mapping com qty=None, intercaladas=None. Mas o WhatsApp Flow
        # estático mostra qty_pattern sempre (não-condicional), então o
        # cidadão pode preencher qty mesmo pra defect non-visual.
        # Normaliza descartando qty/intercaladas pra non-visual antes do
        # lookup; previne KeyError sem mudar UX do Flow.
        defeito = state.data.get("luminaria_defeito")
        if defeito in {"Pendurada", "Danificada", "Com ruído"}:
            qty, intercaladas = None, None
        else:
            qty = state.data.get("luminaria_quantidade")
            intercaladas = state.data.get("luminaria_intercaladas_bloco")
        key = (defeito, qty, intercaladas)
        if key not in mapping:
            logger.warning(
                f"_classifica_defeito: combinação inválida {key}, "
                f"fallback pro próprio defect_type"
            )
            state.data["luminaria_defeito_classificado"] = defeito
            return
        state.data["luminaria_defeito_classificado"] = mapping[key]
        logger.info(
            f"Defeito classificado: {state.data['luminaria_defeito_classificado']}"
        )

    def _is_praca_address(self, state: ServiceState) -> bool:
        return self._logradouro_indicador_praca(state)

    def _logradouro_indicador_praca(self, state: ServiceState) -> bool:
        address = state.data.get("address") or {}
        street = address.get("logradouro_nome_ipp") or address.get("logradouro") or ""
        return (
            _strip_accents(street).startswith("praca ")
            or _strip_accents(street) == "praca"
        )

    def _esta_na_praca(self, state: ServiceState) -> bool:
        return state.data.get(
            "luminaria_localizacao"
        ) == "Praça" or self._is_praca_address(state)

    def _nome_praca(self, state: ServiceState) -> str:
        address = state.data.get("address") or {}
        return address.get("logradouro_nome_ipp") or address.get("logradouro") or ""

    def _normalize_payload_aliases(self, state: ServiceState) -> None:
        """Aceita variações naturais do agente sem mudar os nomes internos/SGRC."""
        if not state.payload:
            return

        # WhatsApp Flow: processar qty_pattern e is_quadra_esportes antes dos aliases
        if state.payload.get("_source") == "whatsapp_flow":
            # Processar qty_pattern
            if "qty_pattern" in state.payload:
                qty = state.payload["qty_pattern"]
                if qty == "uma":
                    state.payload["luminaria_quantidade"] = "uma"
                elif qty in ["bloco", "intercaladas"]:
                    state.payload["luminaria_quantidade"] = "grupo"
                    state.payload["luminaria_intercaladas_bloco"] = qty

            # Processar is_quadra_esportes: se sim, sobrescrever location
            if state.payload.get("is_quadra_esportes") == "sim":
                state.payload["location"] = "Quadra de esportes"

        aliases = {
            "endereco": "address",
            "endereço": "address",
            "luminaria_endereco": "address",
            "defeito": "luminaria_defeito",
            "tipo_defeito": "luminaria_defeito",
            "defect_type": "luminaria_defeito",  # WhatsApp Flow
            "quantidade": "luminaria_quantidade",
            "qtd": "luminaria_quantidade",
            "localizacao": "luminaria_localizacao",
            "localização": "luminaria_localizacao",
            "location": "luminaria_localizacao",  # WhatsApp Flow
            "onde": "luminaria_localizacao",
            "intercaladas_bloco": "luminaria_intercaladas_bloco",
            "bloco_intercaladas": "luminaria_intercaladas_bloco",
        }

        for alias, canonical in aliases.items():
            value = state.payload.get(alias)
            # Pula valores vazios — ex: o campo OPCIONAL `endereco` do Flow
            # submetido em branco viria como "". Sem esse guard, `address=""`
            # faria _collect_address tratar como tentativa de geocode e emitir
            # "Endereço não pode estar vazio" em vez de pedir o endereço.
            if value not in (None, "") and canonical not in state.payload:
                state.payload[canonical] = value

    def _needs_quadra_question(self, state: ServiceState) -> bool:
        if state.data.get("reparo_luminaria_endereco_especial_executado"):
            return False
        if state.data.get("luminaria_localizacao") == "Quadra de esportes":
            state.data["reparo_luminaria_quadra_esportes"] = True
            state.data["reparo_luminaria_endereco_especial_executado"] = True
            return False
        return (
            self._is_praca_address(state)
            or state.data.get("luminaria_localizacao") == "Praça"
        )

    async def _load_service_knowledge(self) -> None:
        """Carrega conhecimento do serviço a partir do hub, sem travar o fluxo se falhar."""
        try:
            request = HubSearchRequest(id=self.knowledge_service_id)
            result = await hub_search_by_id(request)

            if not (result and result.get("id")):
                search_request = HubSearchRequest(
                    q="Reparo de luminária",
                    per_page=1,
                    threshold_hybrid=0.8,
                )
                search_result = await hub_search(search_request)
                results = (search_result or {}).get("results_clean") or []
                result = results[0] if results else None

            if result and result.get("id"):
                self.service_knowledge = result
                logger.info(
                    "[KNOWLEDGE] Conhecimento carregado sobre reparo de luminária"
                )
            else:
                logger.warning(
                    "[KNOWLEDGE] Não foi possível carregar conhecimento do Typesense"
                )

        except Exception as exc:
            logger.error(f"[KNOWLEDGE] Erro ao buscar conhecimento: {exc}")

    @handle_errors
    async def _initialize_workflow(self, state: ServiceState) -> ServiceState:
        logger.info("[ENTRADA] _initialize_workflow")
        self._normalize_payload_aliases(state)

        # Semente de prefill do Flow (auto-send pós-confirmação): captura o que o
        # agente extraiu da 1ª mensagem do cidadão (defeito/local/quantidade) numa
        # chave DEDICADA `flow_prefill_seed`. Crucial: NÃO gravar em
        # `luminaria_defeito` — isso dispararia `ja_tem_dados_defeito` no
        # multi_step_service (app.py) e SUPRIMIRIA o envio do Flow. Sem este stash
        # a extração inicial se perdia entre o "É este serviço?" e o auto-send
        # (turn 1 pausa em _show_service_summary, antes de _collect_defect persistir)
        # → o formulário abria vazio.
        if state.payload and state.payload.get("_source") != "whatsapp_flow":
            seed = dict(state.data.get("flow_prefill_seed") or {})
            # Captura tanto os nomes internos (luminaria_*) quanto os canônicos do
            # Flow (defect_type/location/qty_pattern) — o agente pode mandar
            # qualquer um. `qty_pattern` NÃO é aliasado p/ luminaria_quantidade fora
            # do source=whatsapp_flow (a conversão "bloco"→grupo+sub-campo não é
            # alias simples), então capturamos cru; o normalizer (no envio) lê
            # ambos os formatos.
            for k in (
                "luminaria_defeito",
                "luminaria_localizacao",
                "luminaria_quantidade",
                "luminaria_intercaladas_bloco",
                "defect_type",
                "location",
                "qty_pattern",
            ):
                v = state.payload.get(k)
                if v not in (None, ""):
                    seed[k] = v
            if seed:
                state.data["flow_prefill_seed"] = seed

        state.data.setdefault("servico_1746_descricao", "Reparo de luminária")
        state.data.setdefault("codigo_servico_1746", "18131")
        state.data.setdefault("identificacao_obrigatoria_1746", False)
        state.data.setdefault("ponto_referencia_obrigatorio", False)

        # WhatsApp Flow: se veio do flow (_source="whatsapp_flow"), os dados de
        # defeito já vêm pré-preenchidos no payload. O agente é responsável por
        # chamar build_whatsapp_flow_envelope ANTES de multi_step_service (via
        # system prompt — Design A, Flow-first).

        if not state.data.get("knowledge_loaded") and not self.service_knowledge:
            await self._load_service_knowledge()
            state.data["knowledge_loaded"] = True

        if self.service_knowledge:
            state.data["service_info"] = {
                "nome": self.service_knowledge.get("title"),
                "resumo": self.service_knowledge.get("resumo"),
                "prazo": self.service_knowledge.get("tempo_atendimento"),
                "custo": self.service_knowledge.get("custo_servico"),
                "servico_nao_cobre": self.service_knowledge.get("servico_nao_cobre"),
            }

        state.agent_response = None
        return state

    @handle_errors
    async def _show_service_summary(self, state: ServiceState) -> ServiceState:
        """
        Exibe o resumo do serviço e pede confirmação antes de enviar o Flow.
        """
        logger.info("[ENTRADA] _show_service_summary")

        # Se já confirmou, pula
        if state.data.get("service_confirmed"):
            return state

        # Se veio do Flow, já foi confirmado implicitamente
        if state.payload.get("_source") == "whatsapp_flow":
            state.data["service_confirmed"] = True
            return state

        # Verificar se há confirmação no payload
        confirmacao = state.payload.get("confirmacao_servico")
        if confirmacao is not None:
            if confirmacao is True or str(confirmacao).lower() in ["sim", "yes", "s"]:
                state.data["service_confirmed"] = True
                state.agent_response = None
                return state
            else:
                # Usuário negou - encerrar workflow
                state.status = "completed"
                state.agent_response = AgentResponse(
                    description="Entendi. Se precisar de outro serviço, estou à disposição!"
                )
                return state

        # Primeira vez - mostrar resumo e pedir confirmação
        service_info = state.data.get("service_info", {})
        nome = service_info.get("nome", "Reparo de luminária")
        resumo = service_info.get("resumo", "")
        prazo = service_info.get("prazo", "")
        nao_cobre = service_info.get("servico_nao_cobre", "")

        description = f"📋 **{nome}**\n\n"
        if resumo:
            description += f"{resumo}\n\n"
        if prazo:
            description += f"⏱️ **Prazo:** {prazo}\n\n"
        if nao_cobre:
            description += f"⚠️ **Este serviço não cobre:** {nao_cobre}\n\n"

        description += "É este serviço que você precisa?"

        state.agent_response = AgentResponse(
            description=description,
            payload_schema=ConfirmacaoServicoPayload.model_json_schema(),
        )

        return state

    def _route_after_service_summary(self, state: ServiceState) -> str:
        """Roteamento após exibir resumo do serviço."""
        # Se confirmou, continua para coletar dados
        if state.data.get("service_confirmed"):
            return "collect_luminaria_details"

        # Se rejeitou, encerra
        if state.status == "completed":
            return END

        # Ainda aguardando confirmação - pausa
        return END

    @handle_errors
    async def _collect_defect(self, state: ServiceState) -> ServiceState:
        logger.info("[ENTRADA] _collect_defect")

        if state.data.get("correction_requested") == "defect":
            for key in [
                "luminaria_defeito",
                "luminaria_quantidade",
                "luminaria_intercaladas_bloco",
                "luminaria_defeito_classificado",
                "ticket_data_confirmed",
            ]:
                state.data.pop(key, None)
            state.data.pop("correction_requested", None)
        elif state.data.get("luminaria_defeito"):
            return state

        if state.payload and "luminaria_defeito" in state.payload:
            try:
                validated = LuminariaDefeitoPayload.model_validate(state.payload)
                state.data["luminaria_defeito"] = validated.luminaria_defeito
                if validated.luminaria_defeito in {
                    "Apagada",
                    "Piscando",
                    "Acesa de dia",
                }:
                    state.agent_response = None
                    return state
                self._classifica_defeito(state)
                state.agent_response = None
                return state
            except Exception as exc:
                state.agent_response = AgentResponse(
                    description=tpl.defeito_invalido(),
                    payload_schema=LuminariaDefeitoPayload.model_json_schema(),
                    error_message=str(exc),
                )
                return state

        state.agent_response = AgentResponse(
            description=tpl.solicitar_defeito(state.data.get("service_info")),
            payload_schema=LuminariaDefeitoPayload.model_json_schema(),
        )
        return state

    @handle_errors
    async def _collect_luminaria_details(self, state: ServiceState) -> ServiceState:
        logger.info("[ENTRADA] _collect_luminaria_details")

        if state.data.get("correction_requested") in {
            "defect",
            "quantity",
            "intercaladas_bloco",
            "location",
        }:
            correction = state.data.pop("correction_requested")
            self._clear_corrected_field(state, correction)

        if state.payload and "luminaria_defeito" in state.payload:
            try:
                validated = LuminariaDefeitoPayload.model_validate(state.payload)
                if state.data.get("luminaria_defeito") != validated.luminaria_defeito:
                    for key in [
                        "luminaria_quantidade",
                        "luminaria_intercaladas_bloco",
                        "luminaria_defeito_classificado",
                    ]:
                        state.data.pop(key, None)
                state.data["luminaria_defeito"] = validated.luminaria_defeito
            except Exception as exc:
                state.agent_response = AgentResponse(
                    description=tpl.defeito_invalido(),
                    payload_schema=LuminariaDefeitoPayload.model_json_schema(),
                    error_message=str(exc),
                )
                return state

        if state.payload and "luminaria_quantidade" in state.payload:
            try:
                validated = LuminariaQuantidadePayload.model_validate(state.payload)
                if (
                    state.data.get("luminaria_quantidade")
                    != validated.luminaria_quantidade
                ):
                    state.data.pop("luminaria_intercaladas_bloco", None)
                    state.data.pop("luminaria_defeito_classificado", None)
                state.data["luminaria_quantidade"] = validated.luminaria_quantidade
            except Exception as exc:
                state.agent_response = AgentResponse(
                    description=tpl.quantidade_invalida(),
                    payload_schema=LuminariaQuantidadePayload.model_json_schema(),
                    error_message=str(exc),
                )
                return state

        if state.payload and "luminaria_intercaladas_bloco" in state.payload:
            try:
                validated = LuminariaIntercaladasBlocoPayload.model_validate(
                    state.payload
                )
                state.data["luminaria_intercaladas_bloco"] = (
                    validated.luminaria_intercaladas_bloco
                )
                state.data.pop("luminaria_defeito_classificado", None)
            except Exception as exc:
                state.agent_response = AgentResponse(
                    description=tpl.intercaladas_bloco_invalido(),
                    payload_schema=LuminariaIntercaladasBlocoPayload.model_json_schema(),
                    error_message=str(exc),
                )
                return state

        if state.payload and "luminaria_localizacao" in state.payload:
            try:
                validated = LuminariaLocalizacaoPayload.model_validate(state.payload)
                state.data["luminaria_localizacao"] = (
                    validated.luminaria_localizacao or "Não sei"
                )
                state.data.pop("reparo_luminaria_endereco_especial_executado", None)
                if validated.luminaria_localizacao == "Quadra de esportes":
                    state.data["reparo_luminaria_quadra_esportes"] = True
                    state.data["reparo_luminaria_endereco_especial_executado"] = True
            except Exception as exc:
                state.agent_response = AgentResponse(
                    description=tpl.localizacao_invalida(),
                    payload_schema=LuminariaLocalizacaoPayload.model_json_schema(),
                    error_message=str(exc),
                )
                return state

        defect = state.data.get("luminaria_defeito")
        if not defect:
            state.agent_response = AgentResponse(
                description=tpl.solicitar_defeito(state.data.get("service_info")),
                payload_schema=LuminariaDefeitoPayload.model_json_schema(),
            )
            return state

        if defect in {"Apagada", "Piscando", "Acesa de dia"} and not state.data.get(
            "luminaria_quantidade"
        ):
            state.agent_response = AgentResponse(
                description=tpl.solicitar_quantidade(),
                payload_schema=LuminariaQuantidadePayload.model_json_schema(),
            )
            return state

        if state.data.get("luminaria_quantidade") == "grupo" and not state.data.get(
            "luminaria_intercaladas_bloco"
        ):
            state.agent_response = AgentResponse(
                description=tpl.solicitar_intercaladas_bloco(),
                payload_schema=LuminariaIntercaladasBlocoPayload.model_json_schema(),
            )
            return state

        if not state.data.get("luminaria_defeito_classificado"):
            self._classifica_defeito(state)

        if "luminaria_localizacao" not in state.data:
            state.agent_response = AgentResponse(
                description=tpl.solicitar_localizacao(),
                payload_schema=LuminariaLocalizacaoPayload.model_json_schema(),
            )
            return state

        state.agent_response = None
        return state

    @handle_errors
    async def _collect_quantity(self, state: ServiceState) -> ServiceState:
        logger.info("[ENTRADA] _collect_quantity")

        if state.data.get("luminaria_defeito") not in {
            "Apagada",
            "Piscando",
            "Acesa de dia",
        }:
            return state
        if state.data.get("luminaria_quantidade"):
            return state

        if state.payload and "luminaria_quantidade" in state.payload:
            try:
                validated = LuminariaQuantidadePayload.model_validate(state.payload)
                state.data["luminaria_quantidade"] = validated.luminaria_quantidade
                if validated.luminaria_quantidade == "uma":
                    self._classifica_defeito(state)
                state.agent_response = None
                return state
            except Exception as exc:
                state.agent_response = AgentResponse(
                    description=tpl.quantidade_invalida(),
                    payload_schema=LuminariaQuantidadePayload.model_json_schema(),
                    error_message=str(exc),
                )
                return state

        state.agent_response = AgentResponse(
            description=tpl.solicitar_quantidade(),
            payload_schema=LuminariaQuantidadePayload.model_json_schema(),
        )
        return state

    @handle_errors
    async def _collect_intercaladas_bloco(self, state: ServiceState) -> ServiceState:
        logger.info("[ENTRADA] _collect_intercaladas_bloco")

        if state.data.get("luminaria_quantidade") != "grupo":
            return state
        if state.data.get("luminaria_intercaladas_bloco"):
            return state

        if state.payload and "luminaria_intercaladas_bloco" in state.payload:
            try:
                validated = LuminariaIntercaladasBlocoPayload.model_validate(
                    state.payload
                )
                state.data["luminaria_intercaladas_bloco"] = (
                    validated.luminaria_intercaladas_bloco
                )
                self._classifica_defeito(state)
                state.agent_response = None
                return state
            except Exception as exc:
                state.agent_response = AgentResponse(
                    description=tpl.intercaladas_bloco_invalido(),
                    payload_schema=LuminariaIntercaladasBlocoPayload.model_json_schema(),
                    error_message=str(exc),
                )
                return state

        state.agent_response = AgentResponse(
            description=tpl.solicitar_intercaladas_bloco(),
            payload_schema=LuminariaIntercaladasBlocoPayload.model_json_schema(),
        )
        return state

    @handle_errors
    async def _collect_location(self, state: ServiceState) -> ServiceState:
        logger.info("[ENTRADA] _collect_location")

        if state.data.get("correction_requested") == "location":
            state.data.pop("luminaria_localizacao", None)
            state.data.pop("reparo_luminaria_quadra_esportes", None)
            state.data.pop("reparo_luminaria_endereco_especial_executado", None)
            state.data.pop("ticket_data_confirmed", None)
            state.data.pop("correction_requested", None)
        elif "luminaria_localizacao" in state.data:
            return state

        if state.payload and "luminaria_localizacao" in state.payload:
            try:
                validated = LuminariaLocalizacaoPayload.model_validate(state.payload)
                state.data["luminaria_localizacao"] = (
                    validated.luminaria_localizacao or "Não sei"
                )
                if validated.luminaria_localizacao == "Quadra de esportes":
                    state.data["reparo_luminaria_quadra_esportes"] = True
                    state.data["reparo_luminaria_endereco_especial_executado"] = True
                state.agent_response = None
                return state
            except Exception as exc:
                state.agent_response = AgentResponse(
                    description=tpl.localizacao_invalida(),
                    payload_schema=LuminariaLocalizacaoPayload.model_json_schema(),
                    error_message=str(exc),
                )
                return state

        state.agent_response = AgentResponse(
            description=tpl.solicitar_localizacao(),
            payload_schema=LuminariaLocalizacaoPayload.model_json_schema(),
        )
        return state

    @handle_errors
    async def _collect_quadra_esportes(self, state: ServiceState) -> ServiceState:
        logger.info("[ENTRADA] _collect_quadra_esportes")

        if not self._needs_quadra_question(state):
            state.data["reparo_luminaria_endereco_especial_executado"] = True
            return state

        if state.payload and "reparo_luminaria_quadra_esportes" in state.payload:
            try:
                validated = QuadraEsportesPayload.model_validate(state.payload)
                state.data["reparo_luminaria_quadra_esportes"] = (
                    validated.reparo_luminaria_quadra_esportes
                )
                state.data["reparo_luminaria_endereco_especial_executado"] = True
                state.agent_response = None
                return state
            except Exception as exc:
                state.agent_response = AgentResponse(
                    description=tpl.confirmar_resposta_invalida(),
                    payload_schema=QuadraEsportesPayload.model_json_schema(),
                    error_message=str(exc),
                )
                return state

        state.agent_response = AgentResponse(
            description=tpl.perguntar_quadra_esportes(),
            payload_schema=QuadraEsportesPayload.model_json_schema(),
        )
        return state

    def _format_ticket_confirmation_data(self, state: ServiceState) -> str:
        dados = []
        dados.append("**SERVICO:** Reparo de luminária")
        dados.append(
            f"**DEFEITO:** {state.data.get('luminaria_defeito_classificado', '')}"
        )
        dados.append(
            f"**LOCALIZAÇÃO:** {state.data.get('luminaria_localizacao', 'Não sei')}"
        )
        if state.data.get("reparo_luminaria_quadra_esportes") is not None:
            dados.append(
                "**DENTRO DE QUADRA DE ESPORTES:** "
                + (
                    "Sim"
                    if state.data.get("reparo_luminaria_quadra_esportes")
                    else "Não"
                )
            )

        if state.data.get("address"):
            dados.append("\n**ENDEREÇO:**")
            dados.append(self.format_address_confirmation(state.data["address"]))

        if state.data.get("ponto_referencia"):
            dados.append(
                f"\n**PONTO DE REFERÊNCIA:**\n{state.data['ponto_referencia']}"
            )

        dados_pessoais = []
        if state.data.get("name"):
            dados_pessoais.append(f"- Nome: {state.data['name']}")
        if state.data.get("cpf"):
            cpf_mascarado = mask_cpf(state.data["cpf"])
            dados_pessoais.append(f"- CPF: {cpf_mascarado}")
        if state.data.get("email"):
            email_mascarado = mask_email(state.data["email"])
            dados_pessoais.append(f"- Email: {email_mascarado}")
        if state.data.get("phone"):
            telefone_mascarado = mask_phone(state.data["phone"])
            dados_pessoais.append(f"- Telefone: {telefone_mascarado}")
        if dados_pessoais:
            dados.append("\n**DADOS DO SOLICITANTE:**")
            dados.extend(dados_pessoais)

        return "\n".join(dados)

    @handle_errors
    async def _confirm_ticket_data(self, state: ServiceState) -> ServiceState:
        logger.info("[ENTRADA] _confirm_ticket_data")

        if state.data.get("ticket_data_confirmed") is True:
            return state

        if "confirmacao" in state.payload or "correcao" in state.payload:
            try:
                validated = TicketDataConfirmationPayload.model_validate(state.payload)

                if validated.confirmacao is True:
                    state.data["ticket_data_confirmed"] = True
                    state.agent_response = None
                    return state

                correction = (validated.correcao or "").lower()
                if not correction:
                    state.agent_response = AgentResponse(
                        description=tpl.solicitar_correcao_dados(),
                        payload_schema=TicketDataConfirmationPayload.model_json_schema(),
                    )
                    return state

                correction_sem_acento = _strip_accents(correction)
                if (
                    "nome" in correction_sem_acento
                    and "praca" in correction_sem_acento
                    and any(
                        termo in correction_sem_acento
                        for termo in ("nao sei", "nao conheco", "nao informei")
                    )
                ):
                    state.agent_response = AgentResponse(
                        description=tpl.confirmar_dados_ticket(
                            self._format_ticket_confirmation_data(state)
                        ),
                        payload_schema=TicketDataConfirmationPayload.model_json_schema(),
                    )
                    return state

                correction_map = [
                    (
                        ("defeito", "luminaria", "luminária"),
                        "defect",
                        "defeito",
                        LuminariaDefeitoPayload,
                    ),
                    (
                        ("quantidade", "grupo", "uma"),
                        "quantity",
                        "quantidade",
                        LuminariaQuantidadePayload,
                    ),
                    (
                        ("intercal", "bloco"),
                        "intercaladas_bloco",
                        "intercaladas_bloco",
                        LuminariaIntercaladasBlocoPayload,
                    ),
                    (
                        (
                            "local",
                            "localizacao",
                            "localização",
                            "quadra",
                            "praca",
                            "praça",
                        ),
                        "location",
                        "localizacao",
                        LuminariaLocalizacaoPayload,
                    ),
                    (
                        ("endereco", "endereço", "rua", "avenida"),
                        "address",
                        "endereco",
                        AddressPayload,
                    ),
                    (
                        ("ponto", "referencia", "referência"),
                        "reference_point",
                        "ponto_referencia",
                        PontoReferenciaPayload,
                    ),
                    (("cpf",), "cpf", "cpf", CPFPayload),
                    (("email", "e-mail"), "email", "email", EmailPayload),
                    (("nome",), "name", "nome", NomePayload),
                ]
                for words, key, template_key, payload_model in correction_map:
                    if any(word in correction for word in words):
                        state.data["correction_requested"] = key
                        state.data.pop("ticket_data_confirmed", None)
                        self._clear_corrected_field(state, key)
                        state.agent_response = AgentResponse(
                            description=tpl.dados_corrigidos_solicitar_campo(
                                template_key
                            ),
                            payload_schema=payload_model.model_json_schema(),
                        )
                        return state

                state.agent_response = AgentResponse(
                    description=tpl.solicitar_correcao_dados(),
                    payload_schema=TicketDataConfirmationPayload.model_json_schema(),
                )
                return state
            except Exception as exc:
                state.agent_response = AgentResponse(
                    description=tpl.confirmar_resposta_invalida(),
                    payload_schema=TicketDataConfirmationPayload.model_json_schema(),
                    error_message=str(exc),
                )
                return state

        state.agent_response = AgentResponse(
            description=tpl.confirmar_dados_ticket(
                self._format_ticket_confirmation_data(state)
            ),
            payload_schema=TicketDataConfirmationPayload.model_json_schema(),
        )
        return state

    def _clear_corrected_field(self, state: ServiceState, key: str) -> None:
        if key == "defect":
            for field in [
                "luminaria_defeito",
                "luminaria_quantidade",
                "luminaria_intercaladas_bloco",
                "luminaria_defeito_classificado",
            ]:
                state.data.pop(field, None)
        elif key == "quantity":
            for field in [
                "luminaria_quantidade",
                "luminaria_intercaladas_bloco",
                "luminaria_defeito_classificado",
            ]:
                state.data.pop(field, None)
        elif key == "intercaladas_bloco":
            for field in [
                "luminaria_intercaladas_bloco",
                "luminaria_defeito_classificado",
            ]:
                state.data.pop(field, None)
        elif key == "location":
            for field in [
                "luminaria_localizacao",
                "reparo_luminaria_quadra_esportes",
                "reparo_luminaria_endereco_especial_executado",
            ]:
                state.data.pop(field, None)
        elif key == "address":
            self._clear_address_data(state)
        elif key == "reference_point":
            for field in ["ponto_referencia", "reference_point_collected"]:
                state.data.pop(field, None)
        elif key == "cpf":
            for field in ["cpf", "cadastro_verificado", "identificacao_pulada"]:
                state.data.pop(field, None)
        elif key == "email":
            for field in ["email", "email_processed", "email_skipped"]:
                state.data.pop(field, None)
        elif key == "name":
            for field in ["name", "name_processed", "name_skipped"]:
                state.data.pop(field, None)

    def _route_after_defect(self, state: ServiceState) -> str:
        if state.agent_response:
            return END
        if state.data.get("luminaria_defeito") in {
            "Apagada",
            "Piscando",
            "Acesa de dia",
        } and not state.data.get("luminaria_quantidade"):
            return "collect_quantity"
        return "collect_location"

    def _route_after_luminaria_details(self, state: ServiceState) -> str:
        if state.agent_response:
            return END
        return "collect_address"

    def _route_after_quantity(self, state: ServiceState) -> str:
        if state.agent_response:
            return END
        if state.data.get("luminaria_quantidade") == "grupo" and not state.data.get(
            "luminaria_intercaladas_bloco"
        ):
            return "collect_intercaladas_bloco"
        return "collect_location"

    def _route_after_intercaladas_bloco(self, state: ServiceState) -> str:
        if state.agent_response:
            return END
        return "collect_location"

    def _route_after_location(self, state: ServiceState) -> str:
        if state.agent_response:
            return END
        return "collect_address"

    def _route_after_address(self, state: ServiceState) -> str:
        if state.agent_response:
            if not (
                state.data.get("address_validated")
                and state.data.get("address_confirmed")
            ):
                return END
        if state.data.get("address_needs_confirmation"):
            return "confirm_address"
        if state.data.get("address_validated") and state.data.get("address_confirmed"):
            return "collect_quadra_esportes"
        return "collect_address"

    def _route_after_confirmation(self, state: ServiceState) -> str:
        if state.data.get("address_max_attempts_reached"):
            return END
        if state.agent_response:
            return END
        if state.data.get("address_confirmed"):
            return "collect_quadra_esportes"
        return "collect_address"

    def _route_after_quadra(self, state: ServiceState) -> str:
        if state.agent_response:
            return END
        return "collect_reference_point"

    def _route_after_reference(self, state: ServiceState) -> str:
        if state.agent_response:
            return END
        return "select_identification_method"

    def _route_after_method_selection(self, state: ServiceState) -> str:
        if state.agent_response:
            return END

        method = state.data.get("identification_method")

        # Se usuário escolheu não se identificar (anônimo), pula para confirmação
        if method == "anonimo":
            return "confirm_ticket_data"
        elif method == "govbr":
            return "authenticate_govbr"
        else:
            return "collect_cpf"

    def _route_after_govbr_auth(self, state: ServiceState) -> str:
        if state.data.get("govbr_authenticated"):
            if not state.data.get("email"):
                return "collect_email"
            if not state.data.get("name"):
                return "collect_name"
            return "confirm_ticket_data"

        if state.agent_response:
            return END

        return "collect_cpf"

    def _route_after_cpf(self, state: ServiceState) -> str:
        if state.data.get("cpf_max_attempts_reached"):
            state.agent_response = None
            return "collect_email"
        if state.agent_response or state.data.get("awaiting_user_memory_confirmation"):
            return END
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
        if not state.data.get("email_processed"):
            return "collect_email"
        if not state.data.get("name_processed"):
            return "collect_name"
        return "confirm_ticket_data"

    def _route_after_email(self, state: ServiceState) -> str:
        if state.data.get("email_max_attempts_reached"):
            state.agent_response = None
            return "collect_name"
        if state.agent_response:
            return END
        if not state.data.get("email_processed"):
            return END
        if state.data.get("name") or state.data.get("name_processed"):
            return "confirm_ticket_data"
        return "collect_name"

    def _route_after_name(self, state: ServiceState) -> str:
        if state.data.get("name_max_attempts_reached"):
            state.agent_response = None
            return "confirm_ticket_data"
        if state.agent_response:
            return END
        if state.data.get("name_processed"):
            return "confirm_ticket_data"
        return END

    def _route_after_ticket_confirmation(self, state: ServiceState) -> str:
        if state.data.get("ticket_data_confirmed") is True:
            return "open_ticket"
        correction = state.data.get("correction_requested")
        if correction == "defect":
            return "collect_luminaria_details"
        if correction == "quantity":
            return "collect_luminaria_details"
        if correction == "intercaladas_bloco":
            return "collect_luminaria_details"
        if correction == "location":
            return "collect_luminaria_details"
        if correction == "address":
            return "collect_address"
        if correction == "reference_point":
            return "collect_reference_point"
        if correction == "cpf":
            return "collect_cpf"
        if correction == "email":
            return "collect_email"
        if correction == "name":
            return "collect_name"
        return END

    def build_graph(self) -> StateGraph[ServiceState]:
        graph = StateGraph(ServiceState)
        graph.add_node("initialize", self._initialize_workflow)
        graph.add_node("show_service_summary", self._show_service_summary)
        graph.add_node("collect_luminaria_details", self._collect_luminaria_details)
        graph.add_node("collect_address", self._collect_address)
        graph.add_node("confirm_address", self._confirm_address)
        graph.add_node("collect_quadra_esportes", self._collect_quadra_esportes)
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

        graph.set_entry_point("initialize")
        graph.add_edge("initialize", "show_service_summary")
        graph.add_conditional_edges(
            "show_service_summary",
            self._route_after_service_summary,
            {"collect_luminaria_details": "collect_luminaria_details", END: END},
        )
        graph.add_conditional_edges(
            "collect_luminaria_details",
            self._route_after_luminaria_details,
            {"collect_address": "collect_address", END: END},
        )
        graph.add_conditional_edges(
            "collect_address",
            self._route_after_address,
            {
                "collect_address": "collect_address",
                "confirm_address": "confirm_address",
                "collect_quadra_esportes": "collect_quadra_esportes",
                END: END,
            },
        )
        graph.add_conditional_edges(
            "confirm_address",
            self._route_after_confirmation,
            {
                "collect_address": "collect_address",
                "collect_quadra_esportes": "collect_quadra_esportes",
                END: END,
            },
        )
        graph.add_conditional_edges(
            "collect_quadra_esportes",
            self._route_after_quadra,
            {"collect_reference_point": "collect_reference_point", END: END},
        )
        graph.add_conditional_edges(
            "collect_reference_point",
            self._route_after_reference,
            {"select_identification_method": "select_identification_method", END: END},
        )
        graph.add_conditional_edges(
            "select_identification_method",
            self._route_after_method_selection,
            {
                "authenticate_govbr": "authenticate_govbr",
                "collect_cpf": "collect_cpf",
                "confirm_ticket_data": "confirm_ticket_data",
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
                "collect_name": "collect_name",
                "confirm_ticket_data": "confirm_ticket_data",
                END: END,
            },
        )
        graph.add_conditional_edges(
            "collect_name",
            self._route_after_name,
            {"confirm_ticket_data": "confirm_ticket_data", END: END},
        )
        graph.add_conditional_edges(
            "confirm_ticket_data",
            self._route_after_ticket_confirmation,
            {
                "collect_luminaria_details": "collect_luminaria_details",
                "collect_address": "collect_address",
                "collect_reference_point": "collect_reference_point",
                "collect_cpf": "collect_cpf",
                "collect_email": "collect_email",
                "collect_name": "collect_name",
                "open_ticket": "open_ticket",
                END: END,
            },
        )
        graph.add_edge("open_ticket", END)
        return graph
