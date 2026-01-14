"""
Workflow de Poda de Árvore

Implementa o fluxo de solicitação de poda com verificação de cadastro.
"""

import time
from datetime import datetime
from typing import Any, Dict, Union

from loguru import logger
from langgraph.graph import StateGraph, END

from src.config.env import PODA_SERVICE_ID
from src.tools.multi_step_service.core.base_workflow import BaseWorkflow, handle_errors
from src.tools.multi_step_service.core.models import ServiceState, AgentResponse

from src.tools.multi_step_service.workflows.poda_de_arvore import templates as tpl
from src.tools.multi_step_service.workflows.poda_de_arvore.integrations import build_ticket_payload
from src.tools.multi_step_service.workflows.poda_de_arvore.state_helpers import ticket_opened, ticket_failed 
from src.tools.multi_step_service.workflows.poda_de_arvore.models import (
    CPFPayload,
    EmailPayload,
    NomePayload,
    AddressPayload,
    AddressData,
    AddressValidationState,
    AddressConfirmationPayload,
    PontoReferenciaPayload,
    TicketDataConfirmationPayload,
)
from src.tools.multi_step_service.workflows.poda_de_arvore.api.api_service import SGRCAPIService, AddressAPIService

from src.utils.typesense_api import HubSearchRequest, hub_search_by_id

from prefeitura_rio.integrations.sgrc import async_new_ticket
from prefeitura_rio.integrations.sgrc.models import Address, Requester
from prefeitura_rio.integrations.sgrc.exceptions import (
    SGRCBusinessRuleException,
    SGRCInvalidBodyException,
    SGRCMalformedBodyException,
    SGRCDuplicateTicketException,
    SGRCEquivalentTicketException,
    SGRCInternalErrorException,
)


class PodaDeArvoreWorkflow(BaseWorkflow):
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

    steps_order = [
        "initialize",
        "collect_address",
        "collect_reference_point",
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
        "collect_cpf": ["collect_email", "collect_name"],
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
    
    async def _load_service_knowledge(self) -> None:
        """Carrega conhecimento sobre o serviço de poda do Typesense."""
        try:            
            request = HubSearchRequest(id=PODA_SERVICE_ID)
            result = await hub_search_by_id(request)
            
            if result and result.get("id"):
                self.service_knowledge = result
                
                logger.info(f"[KNOWLEDGE] Conhecimento carregado sobre poda de árvore")
                logger.debug(f"[KNOWLEDGE] Dados: {self.service_knowledge}")
            else:
                logger.warning("[KNOWLEDGE] Não foi possível carregar conhecimento do Typesense")
                
        except Exception as e:
            logger.error(f"[KNOWLEDGE] Erro ao buscar conhecimento: {e}")
            # Continua sem o conhecimento adicional

    def _has_valid_confirmed_address(self, state: ServiceState) -> bool:
        return (
            state.data.get("address_validated")
            and state.data.get("address_confirmed")
            and not state.data.get("ticket_created")
            and not state.data.get("error")
            and not state.data.get("awaiting_user_memory_confirmation")
            and not state.data.get("awaiting_address_memory_confirmation")
            and state.payload
        )

    def _clear_address_data(self, state: ServiceState) -> None:
        keys = [
            "address", "address_temp", "address_validated", "address_confirmed",
            "address_needs_confirmation", "address_validation", "last_address_text"
        ]
        for key in keys:
            state.data.pop(key, None)

    def format_address_confirmation(self, address: dict) -> str:
        parts = []

        logradouro = address.get("logradouro_nome_ipp") or address.get("logradouro")
        if logradouro:
            parts.append(f"- Logradouro: {logradouro}")

        if address.get("numero"):
            parts.append(f"- Número: {address['numero']}")

        bairro = address.get("bairro_nome_ipp") or address.get("bairro")
        if bairro:
            parts.append(f"- Bairro: {bairro}")

        parts.append(
            f"- Cidade: {address.get('cidade', 'Rio de Janeiro')}, "
            f"{address.get('estado', 'RJ')}")

        return "\n".join(parts)

    def increment_attempts(self, state: ServiceState, key: str) -> int:
        attempts = state.data.get(key, 0) + 1
        state.data[key] = attempts
        return attempts
    
    def _reset_previous_session_flags(self, state: ServiceState) -> None:
        if state.payload or state.data.get("restarting_after_error"):
            return

        if not any(
            state.data.get(k)
            for k in (
                "ticket_created",
                "error",
                "awaiting_user_memory_confirmation",
                "awaiting_address_memory_confirmation",
            )
        ):
            return

        logger.info("[NOVO ATENDIMENTO] Resetando flags da sessão anterior")

        for key in [
            "ticket_created", "error", "address_confirmed", "address_validated",
            "awaiting_address_memory_confirmation", "awaiting_user_memory_confirmation",
            "reference_point_collected", "need_reference_point", "ponto_referencia",
            "cadastro_verificado", "address_needs_confirmation",
            "address_validation", "address_max_attempts_reached"
        ]:
            state.data.pop(key, None)

        if state.data.get("cpf") or state.data.get("email") or state.data.get("name"):
            state.data["personal_data_needs_confirmation"] = True
    
    def _handle_address_from_memory(self, state: ServiceState) -> bool:
        if state.data.get("awaiting_address_memory_confirmation") and "confirmacao" in state.payload:
            try:
                validated = AddressConfirmationPayload.model_validate(state.payload)
                state.data.pop("awaiting_address_memory_confirmation", None)

                if validated.confirmacao:
                    logger.info("[MEMÓRIA] Usuário confirmou usar endereço anterior")
                    addr = state.data.get("address") or state.data.get("address_temp")

                    if addr:
                        state.data["address"] = addr
                        state.data["address_confirmed"] = True
                        state.data["address_validated"] = True
                        state.data["need_reference_point"] = True
                        state.data.pop("address_temp", None)
                else:
                    logger.info("[MEMÓRIA] Usuário recusou endereço anterior")
                    self._clear_address_data(state)

                state.agent_response = None
                return True

            except Exception as e:
                logger.error(f"Erro ao processar confirmação de endereço da memória: {e}")
                self._clear_address_data(state)

        if (
            not state.payload
            and (state.data.get("address") or state.data.get("address_temp"))
            and not state.data.get("awaiting_address_memory_confirmation")
            and not state.data.get("awaiting_user_memory_confirmation")
        ):
            logger.info("[MEMÓRIA] Detectado endereço de atendimento anterior")

            addr = state.data.get("address") or state.data.get("address_temp")
            state.data["awaiting_address_memory_confirmation"] = True

            state.agent_response = AgentResponse(
                description=tpl.endereco_historico(self.format_address_confirmation(addr)),
                payload_schema=AddressConfirmationPayload.model_json_schema()
            )
            return True

        return False
    
    def _handle_restart_after_error(self, state: ServiceState) -> bool:
        if not state.data.get("restarting_after_error"):
            return False

        state.data.pop("restarting_after_error", None)
        error_msg = state.data.pop("error_message", "Não foi possível criar o ticket.")

        state.agent_response = AgentResponse(
            description=tpl.reiniciar_apos_erro(error_msg),
            payload_schema=AddressPayload.model_json_schema()
        )
        return True

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
                "custo": self.service_knowledge.get("custo_servico")
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
    async def _collect_address(self, state: ServiceState) -> ServiceState:
        """Coleta endereço do usuário para a solicitação."""
        logger.info("[ENTRADA] _collect_address")
        
        self._reset_previous_session_flags(state)
        
        if self._has_valid_confirmed_address(state):
            return state

        if (state.data.get("address_needs_confirmation") or state.data.get("awaiting_user_memory_confirmation")):
            return state
        
        if self._handle_address_from_memory(state):
            return state
        
        if self._handle_restart_after_error(state):
            return state
        
        if self._has_valid_confirmed_address(state):
            return state

        if "address_validation" not in state.data:
            state.data["address_validation"] = AddressValidationState().model_dump()
        
        validation_state = AddressValidationState(**state.data["address_validation"])
        
        if state.payload and "address" in state.payload:
            try:
                validated_data = AddressPayload.model_validate(state.payload)
                address_text = validated_data.address.strip()
                
                if not address_text:
                    raise ValueError("Endereço não pode estar vazio")
                
                validation_state.attempts += 1
                state.data["last_address_text"] = address_text
                
                if validation_state.attempts >= validation_state.max_attempts:
                    state.data["address_validation"] = validation_state.model_dump()
                    state.data["address_max_attempts_reached"] = True
                    state.agent_response = AgentResponse(
                        description=tpl.endereco_maximo_tentativas(),
                        error_message="Máximo de tentativas excedido"
                    )
                    return state
                
                address_to_google = f"{address_text}, Rio de Janeiro - RJ"
                
                if self.use_fake_api:
                    address_info = {
                        "valid": True,
                        "logradouro": "Rua Teste",
                        "numero": "123",
                        "bairro": "Centro",
                        "cidade": "Rio de Janeiro",
                        "estado": "RJ",
                        "latitude": -22.9068,
                        "longitude": -43.1729
                    }
                else:
                    address_info = await self.address_service.google_geolocator(address_to_google)
                
                if not address_info.get("valid"):
                    validation_state.last_error = address_info.get("error")
                    state.data["address_validation"] = validation_state.model_dump()
                    
                    state.agent_response = AgentResponse(
                        description=tpl.endereco_nao_localizado(validation_state.attempts, validation_state.max_attempts),
                        payload_schema=AddressPayload.model_json_schema(),
                        error_message=address_info.get("error"),
                    )
                    return state
                
                # Obtém informações do IPP
                if not self.use_fake_api and address_info.get("latitude") and address_info.get("longitude"):
                    ipp_info = await self.address_service.get_endereco_info(
                        latitude=address_info["latitude"],
                        longitude=address_info["longitude"],
                        logradouro_google=address_info.get("logradouro"),
                        bairro_google=address_info.get("bairro")
                    )
                    
                    # Mescla informações do IPP se disponíveis
                    if ipp_info and not ipp_info.get("error"):
                        address_info.update(ipp_info)
                    
                    # Valida se conseguiu identificar códigos IPP necessários
                    if not address_info.get("logradouro_id") or address_info.get("bairro_id") in [None, "0", ""]:
                        logger.warning("Não foi possível identificar códigos IPP válidos")
                        validation_state.last_error = "Não foi possível identificar o endereço na base de dados da Prefeitura"
                        state.data["address_validation"] = validation_state.model_dump()
                        
                        # Verifica tentativas
                        if validation_state.attempts >= validation_state.max_attempts:
                            state.agent_response = AgentResponse(
                                description=tpl.endereco_maximo_tentativas(),
                                error_message="Máximo de tentativas excedido"
                            )
                            state.data["address_max_attempts_reached"] = True
                        else:
                            state.agent_response = AgentResponse(
                                description=tpl.endereco_nao_localizado(validation_state.attempts, validation_state.max_attempts),
                                payload_schema=AddressPayload.model_json_schema(),
                                error_message="Endereço não identificado na base IPP"
                            )
                        return state
                
                # Formata número se necessário
                numero_formatado = str(address_info.get("numero", "")).split(".")[0] if address_info.get("numero") else ""
                
                # Cria objeto AddressData
                address_data = AddressData(
                    logradouro=address_info.get("logradouro", ""),
                    numero=numero_formatado,
                    bairro=address_info.get("bairro", ""),
                    cep=address_info.get("cep"),
                    cidade=address_info.get("cidade", "Rio de Janeiro"),
                    estado=address_info.get("estado", "RJ"),
                    latitude=address_info.get("latitude"),
                    longitude=address_info.get("longitude"),
                    logradouro_id_ipp=address_info.get("logradouro_id"),
                    logradouro_nome_ipp=address_info.get("logradouro_nome"),
                    bairro_id_ipp=address_info.get("bairro_id"),
                    bairro_nome_ipp=address_info.get("bairro_nome"),
                    formatted_address=address_info.get("formatted_address", address_text),
                    original_text=address_text
                )
                
                # Armazena dados do endereço temporáriamente para confirmação
                state.data["address_temp"] = address_data.model_dump()
                state.data["address_needs_confirmation"] = True
                validation_state.validated = True
                state.data["address_validation"] = validation_state.model_dump()
                
                logger.info(f"Endereço identificado: {address_text}")
                state.agent_response = None  # Não envia resposta, vai para confirmação
                return state
                
            except Exception as e:
                logger.error(f"Erro ao processar endereço: {e}")
                validation_state.last_error = str(e)
                state.data["address_validation"] = validation_state.model_dump()
                
                if validation_state.attempts >= validation_state.max_attempts:
                    state.agent_response = AgentResponse(
                        description=tpl.endereco_maximo_tentativas(),
                        error_message="Máximo de tentativas excedido"
                    )
                    state.data["address_max_attempts_reached"] = True
                else:
                    state.agent_response = AgentResponse(
                        description=tpl.endereco_erro_processamento(validation_state.attempts, validation_state.max_attempts),
                        payload_schema=AddressPayload.model_json_schema(),
                        error_message=f"Erro: {str(e)}"
                    )
                return state
        
        state.agent_response = AgentResponse(
            description=tpl.solicitar_endereco(),
            payload_schema=AddressPayload.model_json_schema()
        )
        
        return state
    
    @handle_errors
    async def _collect_reference_point(self, state: ServiceState) -> ServiceState:
        """Coleta ponto de referência opcional."""
        logger.info("[ENTRADA] _collect_reference_point")
        
        if state.payload and "correcao" in state.payload:
            return state
        
        # Se veio de correção de ponto de referência, limpa a flag
        if state.data.get("correction_requested") == "reference_point":
            state.data.pop("correction_requested", None)
        # Se está em processo de correção de outro campo, não processa aqui
        elif state.data.get("correction_requested"):
            return state
        
        if state.data.get("reference_point_collected") or not state.data.get("need_reference_point"):
            return state
        
        if state.payload and "ponto_referencia" not in state.payload and state.agent_response is None:
            state.agent_response = AgentResponse(
                description=tpl.solicitar_ponto_referencia(),
                payload_schema=PontoReferenciaPayload.model_json_schema(),
            )
            return state
        
        if state.payload and "ponto_referencia" in state.payload:
            try:
                validated = PontoReferenciaPayload.model_validate(state.payload)
                
                ponto_ref = validated.ponto_referencia
                if ponto_ref and isinstance(ponto_ref, str) and ponto_ref.strip():
                    state.data["ponto_referencia"] = ponto_ref
                    logger.info(f"Ponto de referência coletado: {ponto_ref}")
                else:
                    state.data["ponto_referencia"] = None
                    logger.info("Usuário optou por não informar ponto de referência")
                
                state.data["reference_point_collected"] = True
                
                # Verificar se há dados pessoais salvos para confirmar
                if state.data.get("cpf") or state.data.get("email") or state.data.get("name"):
                    state.data["personal_data_needs_confirmation"] = True
                
                state.agent_response = None
                return state
                
            except Exception as e:
                logger.warning(f"Erro ao processar ponto de referência: {e}")
                state.data["ponto_referencia"] = None
                state.data["reference_point_collected"] = True
                
                # Verificar se há dados pessoais salvos para confirmar
                if state.data.get("cpf") or state.data.get("email") or state.data.get("name"):
                    state.data["personal_data_needs_confirmation"] = True
                
                state.agent_response = None
                return state
        
        state.agent_response = AgentResponse(
            description=tpl.solicitar_ponto_referencia(),
            payload_schema=PontoReferenciaPayload.model_json_schema(),
        )
        
        return state
    
    @handle_errors
    async def _collect_cpf(
        self, 
        state: ServiceState
    ) -> ServiceState:
        logger.info("[ENTRADA] _collect_cpf")
        
        if state.data.get("correction_requested") == "cpf":
            state.data.pop("cpf", None)
            state.data.pop("identificacao_pulada", None)
            state.data.pop("correction_requested", None)
            logger.info("[CPF] Correção solicitada - reprocessando...")
        elif state.data.get("cpf") or state.data.get("identificacao_pulada"):
            logger.info("[CPF] Já processado, pulando...")
            return state
        
        if state.data.get("awaiting_user_memory_confirmation"):
            if "confirmacao" in state.payload:
                try:
                    validated = AddressConfirmationPayload.model_validate(state.payload)
                    state.data.pop("awaiting_user_memory_confirmation", None)
                    
                    if validated.confirmacao:
                        logger.info("[MEMÓRIA] Usuário confirmou usar dados pessoais anteriores")
                        state.data["cadastro_verificado"] = True
                        state.agent_response = None
                        return state
                    logger.info("[MEMÓRIA] Usuário recusou dados pessoais anteriores. Limpando...")

                    for key in ["cpf", "email", "name", "phone", "cadastro_verificado", 
                        "identificacao_pulada", "cpf_attempts", "email_attempts", "name_attempts",
                        "awaiting_user_memory_confirmation"]:
                        state.data.pop(key, None)

                    state.agent_response = AgentResponse(
                        description=tpl.solicitar_cpf(),
                        payload_schema=CPFPayload.model_json_schema(),
                    )
                    return state
                    
                except Exception as e:
                    logger.error(f"Erro ao processar confirmação de dados pessoais: {e}")
            else:
                user_input = str(state.payload.get("email", "")).lower() if "email" in state.payload else ""
                
                if user_input == "" and "email" in state.payload:
                    logger.info("[MEMÓRIA] Usuário optou por pular dados pessoais (avançar)")
                    state.data.pop("awaiting_user_memory_confirmation", None)

                    for key in ["cpf", "email", "name", "phone", "cadastro_verificado"]:
                        state.data.pop(key, None)

                    state.agent_response = None
                    return state
                logger.info("[MEMÓRIA] Usuário não enviou confirmação - repetindo pergunta")
                   
        payload_is_empty = not state.payload or (len(state.payload) == 1 and "ponto_referencia" in state.payload)
        
        should_ask_about_data = (
            state.data.get("address_confirmed") 
            or state.data.get("personal_data_needs_confirmation")
            or (
                state.data.get("cpf") == "skip"
                and not state.data.get("name")
                and not state.data.get("email")
            )
        )
        
        if (((payload_is_empty and not state.data.get("awaiting_user_memory_confirmation")) or 
            (state.data.get("awaiting_user_memory_confirmation") and "confirmacao" not in state.payload)) and 
            should_ask_about_data):
            masked_data = []
            
            if state.data.get("name"):
                parts = state.data["name"].split()
                nome_mask = (
                    f"{parts[0]} {parts[-1][0]}."
                    if len(parts) > 1 else parts[0]
                )
                
                masked_data.append(f"- Nome: {nome_mask}")
                
            if state.data.get("cpf") and state.data["cpf"] != "skip":
                cpf = state.data["cpf"]
                cpf_mask = (
                    f"XXX.{cpf[3:6]}.{cpf[6:9]}-XX"
                    if len(cpf) == 11 else "XXX"
                )

                masked_data.append(f"- CPF: {cpf_mask}")
                
            if state.data.get("email"):
                user, _, domain = state.data["email"].partition("@")
                email_mask = (
                    f"{user[:2]}***@{domain}"
                    if len(user) > 2 else f"{user}***@{domain}"
                )

                masked_data.append(f"- Email: {email_mask}")
            
            if masked_data:
                message = (
                    "Por questões de segurança, não posso exibir dados sensíveis completos.\n\n"
                    if state.data.get("awaiting_user_memory_confirmation") and state.payload
                    else ""
                )

                message += tpl.confirmar_dados_salvos(masked_data)
                                
                state.data["awaiting_user_memory_confirmation"] = True
                state.data.pop("personal_data_needs_confirmation", None)
                
                state.agent_response = AgentResponse(
                    description=message,
                    payload_schema=AddressConfirmationPayload.model_json_schema()
                )
                return state
        
        if state.payload and "cpf" in state.payload:
            try:
                validated = CPFPayload.model_validate(state.payload)
                cpf_novo = validated.cpf
                
                if not cpf_novo:
                    state.data["cpf"] = "skip" 
                    state.data["identificacao_pulada"] = True
                    logger.info("Usuário optou por não se identificar")
                    state.agent_response = None
                    return state
                
                state.data["cpf"] = cpf_novo
                logger.info(f"CPF coletado: {cpf_novo}")

                if not self.use_fake_api:
                    try:
                        user_info = await self.api_service.get_user_info(cpf_novo)

                        if user_info.get("email"):
                            state.data["email"] = user_info["email"].strip().lower()
                        
                        if user_info.get("name"):
                            state.data["name"] = user_info["name"].strip()
                        
                        if user_info.get("phones"):
                            state.data["phone"] = str(user_info["phones"][0]).strip() if user_info["phones"][0] else ""
                        
                        state.data["cadastro_verificado"] = True
                        logger.info("Cadastro encontrado")
                        
                    except AttributeError as e:
                        logger.error(f"Erro ao processar resposta da API de cadastro: {str(e)}")
                        state.data["cadastro_verificado"] = False
                        
                    except (ConnectionError, TimeoutError) as e:
                        logger.error(f"API de cadastro indisponível: {str(e)}")
                        state.data["cadastro_verificado"] = False
                        state.data["api_indisponivel"] = True
                        
                    except Exception as e:
                        logger.info(f"Usuário não encontrado no cadastro ou erro na consulta: {str(e)}")
                        state.data["cadastro_verificado"] = False
                
                state.agent_response = None
                return state
            
            except Exception as e:
                attempts = self.increment_attempts(state, "cpf_attempts")
                
                if attempts >= 3:
                    state.data["cpf"] = "skip"
                    state.data["identificacao_pulada"] = True
                    state.data["cpf_max_attempts_reached"] = True

                    state.agent_response = AgentResponse(
                        description=tpl.maximo_tentativas_excedido(),
                        error_message="Máximo de tentativas excedido"
                    )
                    return state
                
                state.agent_response = AgentResponse(
                    description=tpl.cpf_invalido(attempts),
                    payload_schema=CPFPayload.model_json_schema(),
                    error_message=str(e),
                )

        if "cpf" in state.data and "cpf" not in state.payload:
            if (
                state.data.get("cpf") == "skip"
                and state.data.get("reference_point_collected")
                and "ponto_referencia" in state.payload
            ):
                state.data.pop("cpf", None)
                state.data.pop("identificacao_pulada", None)
            else:
                return state

        if state.agent_response and state.agent_response.error_message:
            return state

        if "cpf" not in state.data:
            state.agent_response = AgentResponse(
                description=tpl.solicitar_cpf(),
                payload_schema=CPFPayload.model_json_schema(),
            )

        return state
    

    @handle_errors
    async def _collect_email(self, state: ServiceState) -> ServiceState:
        """Coleta email do usuário (opcional)."""
        logger.info("[ENTRADA] _collect_email")
        
        # Se veio de correção, limpa a flag
        if state.data.get("correction_requested") == "email":
            state.data.pop("correction_requested", None)
        # Se já processou email, retorna sem fazer nada
        elif state.data.get("email_processed"):
            return state
        
        if state.payload and "email" in state.payload:
            email_value = state.payload.get("email")
            if not email_value or (isinstance(email_value, str) and email_value.strip() == ""):
                state.data["email_skipped"] = True
                state.data["email_processed"] = True
                logger.info("Usuário optou por não informar email")
                state.agent_response = None
                return state
            
            try:
                validated_data = EmailPayload.model_validate(state.payload)
                email_novo = validated_data.email
                
                state.data["email"] = email_novo
                state.data["email_processed"] = True
                logger.info(f"Email coletado: {email_novo}")
                
                state.agent_response = None
                return state
            
            except Exception as e:
                attempts = self.increment_attempts(state, "email_attempts")
                logger.info(f"[EMAIL] Tentativa {attempts}/3 - Erro: {str(e)}")
                
                if attempts >= 3:
                    state.data["email_skipped"] = True
                    state.data["email_processed"] = True
                    state.data["email_max_attempts_reached"] = True
                    logger.warning(f"Email: máximo de tentativas ({attempts}) excedido. Pulando email.")
                    state.agent_response = AgentResponse(
                        description=tpl.email_maximo_tentativas(),
                        error_message="Máximo de tentativas excedido"
                    )
                    return state
                
                state.agent_response = AgentResponse(
                    description=tpl.email_invalido(attempts),
                    payload_schema=EmailPayload.model_json_schema(),
                    error_message=str(e),
                )
                return state

        if state.data.get("email_processed") and "email" not in state.payload:
            logger.info("Email já processado, prosseguindo")
            return state

        if state.agent_response and state.agent_response.error_message:
            return state

        if not state.data.get("email_processed"):
            state.agent_response = AgentResponse(
                description=tpl.solicitar_email(),
                payload_schema=EmailPayload.model_json_schema(),
            )

        return state
    

    @handle_errors
    async def _collect_name(self, state: ServiceState) -> ServiceState:
        """Coleta nome do usuário (opcional)."""
        logger.info("[ENTRADA] _collect_name")

        # Se veio de correção, limpa a flag
        if state.data.get("correction_requested") == "name":
            state.data.pop("correction_requested", None)
        elif state.data.get("name_processed"):
            return state
        
        # Só processa se o payload tiver o campo esperado
        if state.payload and "name" in state.payload:
            name_value = state.payload.get("name")
            if not name_value or (isinstance(name_value, str) and name_value.strip() == ""):
                state.data["name_skipped"] = True
                state.data["name_processed"] = True
                logger.info("Usuário optou por não informar nome")
                state.agent_response = None
                return state
            
            try:
                validated_data = NomePayload.model_validate(state.payload)
                nome_novo = validated_data.name
                    
                state.data["name"] = nome_novo
                state.data["name_processed"] = True
                logger.info(f"Nome coletado: {nome_novo}")

                state.agent_response = None
                return state
            
            except Exception as e:
                attempts = self.increment_attempts(state, "name_attempts")
                
                if attempts >= 3:
                    state.data["name_skipped"] = True
                    state.data["name_processed"] = True
                    state.data["name_max_attempts_reached"] = True
                    logger.warning(f"Nome: máximo de tentativas ({attempts}) excedido. Pulando nome.")

                    state.agent_response = AgentResponse(
                        description=tpl.nome_maximo_tentativas(),
                        error_message="Máximo de tentativas excedido"
                    )
                    return state
                
                state.agent_response = AgentResponse(
                    description=tpl.nome_invalido(attempts),
                    payload_schema=NomePayload.model_json_schema(),
                    error_message=str(e),
                )
                return state

        if state.data.get("name_processed") and "name" not in state.payload:
            logger.info("Nome já processado, prosseguindo")
            return state

        if state.agent_response and state.agent_response.error_message:
            return state

        if not state.data.get("name_processed"):
            state.agent_response = AgentResponse(
                description=tpl.solicitar_nome(),
                payload_schema=NomePayload.model_json_schema(),
            )

        return state

    
    async def new_ticket(
        self,
        classification_code: str,
        description: str = "",
        address: Address = None,
        date_time: Union[datetime, str] = None,
        requester: Requester = None,
        occurrence_origin_code: str = "28",
        specific_attributes: Dict[str, Any] = None,
    ):
        """Cria um novo ticket no SGRC."""
        start_time = time.time()
        end_time = None

        try:
            ticket = await async_new_ticket(
                classification_code=classification_code,
                description=description,
                address=address,
                date_time=date_time,
                requester=requester,
                occurrence_origin_code=occurrence_origin_code,
                specific_attributes=specific_attributes or {},
            )

            end_time = time.time()
            logger.info(f"Ticket criado com sucesso. Protocol ID: {ticket.protocol_id}. Tempo: {end_time - start_time:.2f}s")
            return ticket
        
        except Exception as exc:
            end_time = end_time if end_time else time.time()
            logger.error(f"Erro ao criar ticket. Tempo: {end_time - start_time:.2f}s. Erro: {exc}")
            raise exc


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
                        logger.info("Usuário disse 'não' sem especificar o que corrigir")
                        state.agent_response = AgentResponse(
                            description=tpl.solicitar_correcao_dados(),
                            payload_schema=TicketDataConfirmationPayload.model_json_schema()
                        )
                        return state
                    
                    # Analisa o que o usuário quer corrigir
                    correcao_lower = correcao_text.lower()
                    
                    # Identifica qual campo precisa ser corrigido
                    if any(word in correcao_lower for word in ["endereço", "endereco", "rua", "avenida", "praça", "local"]):
                        # Volta para coletar endereço novamente
                        state.data["correction_requested"] = "address"
                        state.data.pop("address_confirmed", None)
                        state.data.pop("address_validated", None)
                        state.data.pop("address", None)
                        state.data.pop("ticket_data_confirmed", None)
                        # Reseta o contador de tentativas de endereço quando vem de correção
                        state.data.pop("address_validation", None)
                        state.agent_response = AgentResponse(
                            description=tpl.dados_corrigidos_solicitar_campo("endereco"),
                            payload_schema=AddressPayload.model_json_schema()
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
                            payload_schema=NomePayload.model_json_schema()
                        )
                        return state
                        
                    elif "cpf" in correcao_lower:
                        # Volta para coletar CPF
                        state.data["correction_requested"] = "cpf"
                        state.data.pop("cpf", None)
                        state.data.pop("cadastro_verificado", None)
                        state.data["ticket_data_confirmed"] = False  # Marca que veio de correção
                        state.agent_response = AgentResponse(
                            description=tpl.dados_corrigidos_solicitar_campo("cpf"),
                            payload_schema=CPFPayload.model_json_schema()
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
                            payload_schema=EmailPayload.model_json_schema()
                        )
                        return state
                        
                    elif any(word in correcao_lower for word in ["ponto", "referência", "referencia"]):
                        # Volta para coletar ponto de referência
                        state.data["correction_requested"] = "reference_point"
                        state.data.pop("ponto_referencia", None)
                        state.data.pop("reference_point_collected", None)
                        state.data.pop("ticket_data_confirmed", None)
                        state.agent_response = AgentResponse(
                            description=tpl.dados_corrigidos_solicitar_campo("ponto_referencia"),
                            payload_schema=PontoReferenciaPayload.model_json_schema()
                        )
                        return state
                    else:
                        # Não conseguiu identificar o que corrigir, pede mais detalhes
                        state.agent_response = AgentResponse(
                            description=tpl.solicitar_correcao_dados(),
                            payload_schema=TicketDataConfirmationPayload.model_json_schema()
                        )
                        return state
                    
            except Exception as e:
                logger.error(f"Erro ao processar confirmação de dados do ticket: {e}")
                state.agent_response = AgentResponse(
                    description=tpl.confirmar_resposta_invalida(),
                    payload_schema=TicketDataConfirmationPayload.model_json_schema(),
                    error_message=f"Resposta inválida: {str(e)}"
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
            dados.append(f"\n📌 **PONTO DE REFERÊNCIA:**\n{state.data['ponto_referencia']}")
        
        # Dados pessoais
        dados_pessoais = []
        if state.data.get("name"):
            dados_pessoais.append(f"- Nome: {state.data['name']}")
        if state.data.get("cpf") and state.data["cpf"] != "skip":
            cpf = state.data["cpf"]
            cpf_formatado = f"{cpf[:3]}.{cpf[3:6]}.{cpf[6:9]}-{cpf[9:]}" if len(cpf) == 11 else cpf
            dados_pessoais.append(f"- CPF: {cpf_formatado}")
        if state.data.get("email"):
            dados_pessoais.append(f"- Email: {state.data['email']}")
        if state.data.get("phone"):
            dados_pessoais.append(f"- Telefone: {state.data['phone']}")
        
        if dados_pessoais:
            dados.append("\n👤 **DADOS DO SOLICITANTE:**")
            dados.extend(dados_pessoais)
        
        # Tipo de serviço
        dados.append("\n🌳 **SERVIÇO:** Poda de Árvore")
        
        dados_formatados = "\n".join(dados)
        
        state.agent_response = AgentResponse(
            description=tpl.confirmar_dados_ticket(dados_formatados),
            payload_schema=TicketDataConfirmationPayload.model_json_schema()
        )
        
        return state

    @handle_errors
    async def _open_ticket(self, state: ServiceState) -> ServiceState:
        """Abre um ticket no SGRC com os dados coletados."""
        logger.info("[ENTRADA] _open_ticket")
        
        if self.use_fake_api:
            protocol = f"FAKE-{int(time.time())}"
            logger.info(f"Ticket fake criado: {protocol}")
            
            return ticket_opened(
                state,
                protocol,
                tpl.solicitacao_criada_sucesso(protocol),
            )
        
        try:
            address, requester, description = build_ticket_payload(state)
            
            ticket = await self.new_ticket(
                classification_code=self.service_id,
                description=description,
                address=address,
                requester=requester,
            )

            return ticket_opened(
                state,
                ticket.protocol_id,
                tpl.solicitacao_criada_sucesso(ticket.protocol_id),
            )
            
        except (
            SGRCBusinessRuleException, 
            SGRCInvalidBodyException, 
            SGRCMalformedBodyException, 
            ValueError
        ) as exc:
            logger.exception(exc)
            return ticket_failed(
                state,
                error_code="erro_interno",
                message=tpl.erro_criar_solicitacao(),
                error_message=str(exc)
            )
        
        except (
            SGRCDuplicateTicketException, 
            SGRCEquivalentTicketException
        ) as exc:
            logger.exception(exc)
            return ticket_failed(
                state,
                error_code="erro_ticket_duplicado",
                description=tpl.solicitacao_existente(getattr(exc, 'protocol_id', 'seu protocolo')),
                error_message=str(exc),
            )
            
        except SGRCInternalErrorException as exc:
            logger.exception(exc)
            return ticket_failed(
                state,
                error_code="erro_sgrc",
                description=tpl.sistema_indisponivel(),
                error_message="Sistema indisponível"
            )
        
        except Exception as exc:
            logger.exception(exc)
            return ticket_failed(
                state,
                error_code="erro_geral",
                description=tpl.erro_geral_chamado(),
                error_message=str(exc),
            )


    # --- Roteamento Condicional ---
    
    @handle_errors
    async def _confirm_address(self, state: ServiceState) -> ServiceState:
        """Confirma o endereço identificado com o usuário."""
        logger.info("[ENTRADA] _confirm_address")
         
        if state.data.get("address_confirmed"):
            return state
        
        if not state.data.get("address_needs_confirmation"):
            logger.warning("_confirm_address chamado mas não há endereço para confirmar")
            return state
        
        if "confirmacao" in state.payload:
            try:
                validated_data = AddressConfirmationPayload.model_validate(state.payload)
                
                if validated_data.confirmacao:
                    state.data["address"] = state.data["address_temp"]
                    state.data["address_confirmed"] = True
                    state.data["address_validated"] = True
                    state.data["address_needs_confirmation"] = False
                    state.data["need_reference_point"] = True

                    logger.info("Endereço confirmado pelo usuário")
                    state.agent_response = None
                    return state
                
                state.data["address_needs_confirmation"] = False
                state.data["address_temp"] = None
                state.data["address_validated"] = False

                logger.info("Endereço não confirmado, solicitando novo endereço")
                
                validation_state = AddressValidationState(**state.data.get("address_validation", {}))
                
                if validation_state.attempts >= validation_state.max_attempts:
                    state.agent_response = AgentResponse(
                        description=tpl.endereco_maximo_tentativas(),
                        error_message="Máximo de tentativas excedido"
                    )
                    state.data["address_max_attempts_reached"] = True
                else:
                    state.agent_response = AgentResponse(
                        description=tpl.solicitar_novo_endereco(validation_state.attempts, validation_state.max_attempts),
                        payload_schema=AddressPayload.model_json_schema()
                    )
                return state
            
            except Exception as e:
                state.agent_response = AgentResponse(
                    description=tpl.confirmar_resposta_invalida(),
                    payload_schema=AddressConfirmationPayload.model_json_schema(),
                    error_message=f"Resposta inválida: {str(e)}"
                )
                return state
        
        if state.data.get("address_temp"):
            address_temp = state.data.get("address_temp", {})
            msg_confirmacao = self.format_address_confirmation(address_temp)
            
            if not state.payload and state.data.get("address_temp"):
                state.agent_response = AgentResponse(
                    description=tpl.endereco_historico(msg_confirmacao),
                    payload_schema=AddressConfirmationPayload.model_json_schema()
                )
            else:
                state.agent_response = AgentResponse(
                    description=tpl.confirmar_endereco(msg_confirmacao),
                    payload_schema=AddressConfirmationPayload.model_json_schema()
                )
        
        return state
    
    def _route_after_address(self, state: ServiceState) -> str:
        logger.info("[ROTEAMENTO] _route_after_address")
        logger.info(f"[STATE.DATA] address_max_attempts_reached: {state.data.get('address_max_attempts_reached')}")
        logger.info(f"[STATE.DATA] address_needs_confirmation: {state.data.get('address_needs_confirmation')}")
        logger.info(f"[STATE.DATA] address_confirmed: {state.data.get('address_confirmed')}")
        logger.info(f"[STATE.DATA] address_validated: {state.data.get('address_validated')}")
        logger.info(f"[STATE] agent_response: {state.agent_response}")
        
        if state.agent_response:
            if not (state.data.get("address_validated") and state.data.get("address_confirmed")):
                return END
        
        if state.data.get("address_needs_confirmation"):
            return "confirm_address"
            
        if state.data.get("address_validated") and state.data.get("address_confirmed"):
            # Se voltou de uma correção e já tem todos os outros dados, vai para confirmação
            if (state.data.get("reference_point_collected") and 
                state.data.get("cpf") and 
                state.data.get("email_processed") and 
                state.data.get("name_processed")):
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
            if (state.data.get("reference_point_collected") and 
                state.data.get("cpf") and 
                state.data.get("email_processed") and 
                state.data.get("name_processed")):
                return "confirm_ticket_data"
            return "collect_reference_point"
        
        return "collect_address"
    
    def _route_after_reference(self, state: ServiceState) -> str:
        logger.info("[ROTEAMENTO] _route_after_reference")

        if state.agent_response:
            return END
        
        return "collect_cpf"
    
    def _route_after_cpf(self, state: ServiceState) -> str:
        logger.info("[ROTEAMENTO] _route_after_cpf")
        logger.info(f"[STATE.DATA] cpf: {state.data.get('cpf')}")
        logger.info(f"[STATE.DATA] cadastro_verificado: {state.data.get('cadastro_verificado')}")
        logger.info(f"[STATE.DATA] cpf_max_attempts_reached: {state.data.get('cpf_max_attempts_reached')}")
        
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
        logger.info(f"[STATE.DATA] email_processed: {state.data.get('email_processed')}")
        logger.info(f"[STATE.DATA] email_max_attempts_reached: {state.data.get('email_max_attempts_reached')}")
        
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
        3. Coleta CPF (opcional) e verifica cadastro
        4. Se não cadastrado ou faltando dados: coleta email e nome (opcionais)
        5. Confirma todos os dados com o usuário
        6. Abre chamado no SGRC
        """
        graph = StateGraph(ServiceState)
        
        # Adiciona os nós
        graph.add_node("initialize", self._initialize_workflow)
        graph.add_node("collect_address", self._collect_address)
        graph.add_node("confirm_address", self._confirm_address)
        graph.add_node("collect_reference_point", self._collect_reference_point)
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
                END: END
            }
        )
        
        graph.add_conditional_edges(
            "confirm_address",
            self._route_after_confirmation,
            {
                "collect_address": "collect_address",
                "collect_reference_point": "collect_reference_point",
                "confirm_ticket_data": "confirm_ticket_data",
                END: END
            }
        )
        
        graph.add_conditional_edges(
            "collect_reference_point",
            self._route_after_reference,
            {
                "collect_cpf": "collect_cpf",
                "confirm_ticket_data": "confirm_ticket_data",
                END: END
            }
        )
        
        graph.add_conditional_edges(
            "collect_cpf",
            self._route_after_cpf,
            {
                "collect_cpf": "collect_cpf",
                "collect_email": "collect_email",
                "collect_name": "collect_name",
                "confirm_ticket_data": "confirm_ticket_data",
                END: END
            }
        )
        
        graph.add_conditional_edges(
            "collect_email",
            self._route_after_email,
            {
                "collect_email": "collect_email",
                "collect_name": "collect_name",
                "confirm_ticket_data": "confirm_ticket_data",
                END: END
            }
        )
        
        graph.add_conditional_edges(
            "collect_name",
            self._route_after_name,
            {
                "collect_name": "collect_name",
                "confirm_ticket_data": "confirm_ticket_data",
                END: END
            }
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
                END: END
            }
        )
        
        # Após open_ticket, sempre termina (já tem agent_response definido)
        graph.add_edge("open_ticket", END)
        
        return graph
