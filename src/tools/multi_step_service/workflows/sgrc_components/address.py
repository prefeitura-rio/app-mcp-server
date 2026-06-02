from loguru import logger

from src.tools.multi_step_service.core.base_workflow import handle_errors
from src.tools.multi_step_service.core.models import AgentResponse, ServiceState
from src.tools.multi_step_service.workflows.sgrc_components.models import (
    AddressConfirmationPayload,
    AddressData,
    AddressPayload,
    AddressValidationState,
    PontoReferenciaPayload,
)


class AddressFlowMixin:
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
            "address",
            "address_temp",
            "address_validated",
            "address_confirmed",
            "address_needs_confirmation",
            "address_validation",
            "last_address_text",
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
            f"{address.get('estado', 'RJ')}"
        )

        return "\n".join(parts)

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
            "ticket_created",
            "error",
            "address_confirmed",
            "address_validated",
            "awaiting_address_memory_confirmation",
            "awaiting_user_memory_confirmation",
            "reference_point_collected",
            "need_reference_point",
            "ponto_referencia",
            "cadastro_verificado",
            "address_needs_confirmation",
            "address_validation",
            "address_max_attempts_reached",
        ]:
            state.data.pop(key, None)

        if state.data.get("cpf") or state.data.get("email") or state.data.get("name"):
            state.data["personal_data_needs_confirmation"] = True

    def _handle_address_from_memory(self, state: ServiceState) -> bool:
        if (
            state.data.get("awaiting_address_memory_confirmation")
            and "confirmacao" in state.payload
        ):
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
                        # Sempre solicita ponto de referência (mesmo quando opcional)
                        # para melhorar precisão da localização
                        state.data["need_reference_point"] = True
                        state.data.pop("address_temp", None)
                else:
                    logger.info("[MEMÓRIA] Usuário recusou endereço anterior")
                    self._clear_address_data(state)

                state.agent_response = None
                return True

            except Exception as e:
                logger.error(
                    f"Erro ao processar confirmação de endereço da memória: {e}"
                )
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
                description=self.templates.endereco_historico(
                    self.format_address_confirmation(addr)
                ),
                payload_schema=AddressConfirmationPayload.model_json_schema(),
            )
            return True

        return False

    def _handle_restart_after_error(self, state: ServiceState) -> bool:
        if not state.data.get("restarting_after_error"):
            return False

        state.data.pop("restarting_after_error", None)
        error_msg = state.data.pop("error_message", "Não foi possível criar o ticket.")

        state.agent_response = AgentResponse(
            description=self.templates.reiniciar_apos_erro(error_msg),
            payload_schema=AddressPayload.model_json_schema(),
        )
        return True

    @handle_errors
    async def _collect_address(self, state: ServiceState) -> ServiceState:
        logger.info("[ENTRADA] _collect_address")

        self._reset_previous_session_flags(state)

        if self._has_valid_confirmed_address(state):
            return state

        if state.data.get("address_needs_confirmation") or state.data.get(
            "awaiting_user_memory_confirmation"
        ):
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
                        "longitude": -43.1729,
                    }
                else:
                    address_info = await self.address_service.google_geolocator(
                        address_to_google
                    )

                if not address_info.get("valid"):
                    validation_state.last_error = address_info.get("error")
                    state.data["address_validation"] = validation_state.model_dump()
                    if validation_state.attempts >= validation_state.max_attempts:
                        state.data["address_max_attempts_reached"] = True
                        state.agent_response = AgentResponse(
                            description=self.templates.endereco_maximo_tentativas(),
                            error_message="Máximo de tentativas excedido",
                        )
                    else:
                        state.agent_response = AgentResponse(
                            description=self.templates.endereco_nao_localizado(
                                validation_state.attempts,
                                validation_state.max_attempts,
                            ),
                            payload_schema=AddressPayload.model_json_schema(),
                            error_message=address_info.get("error"),
                        )
                    return state

                if (
                    not self.use_fake_api
                    and address_info.get("latitude")
                    and address_info.get("longitude")
                ):
                    ipp_info = await self.address_service.get_endereco_info(
                        latitude=address_info["latitude"],
                        longitude=address_info["longitude"],
                        logradouro_google=address_info.get("logradouro"),
                        bairro_google=address_info.get("bairro"),
                    )

                    if ipp_info and not ipp_info.get("error"):
                        address_info.update(ipp_info)

                    if not address_info.get("logradouro_id") or address_info.get(
                        "bairro_id"
                    ) in [None, "0", ""]:
                        logger.warning(
                            "Não foi possível identificar códigos IPP válidos"
                        )
                        validation_state.last_error = "Não foi possível identificar o endereço na base de dados da Prefeitura"
                        state.data["address_validation"] = validation_state.model_dump()

                        if validation_state.attempts >= validation_state.max_attempts:
                            state.agent_response = AgentResponse(
                                description=self.templates.endereco_maximo_tentativas(),
                                error_message="Máximo de tentativas excedido",
                            )
                            state.data["address_max_attempts_reached"] = True
                        else:
                            state.agent_response = AgentResponse(
                                description=self.templates.endereco_nao_localizado(
                                    validation_state.attempts,
                                    validation_state.max_attempts,
                                ),
                                payload_schema=AddressPayload.model_json_schema(),
                                error_message="Endereço não identificado na base IPP",
                            )
                        return state

                numero_formatado = (
                    str(address_info.get("numero", "")).split(".")[0]
                    if address_info.get("numero")
                    else ""
                )

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
                    formatted_address=address_info.get(
                        "formatted_address", address_text
                    ),
                    original_text=address_text,
                )

                state.data["address_temp"] = address_data.model_dump()
                state.data["address_needs_confirmation"] = True
                validation_state.validated = True
                state.data["address_validation"] = validation_state.model_dump()

                # Sempre solicita ponto de referência (mesmo quando opcional)
                # para melhorar precisão da localização
                state.data["need_reference_point"] = True

                logger.info(f"Endereço identificado: {address_text}")
                state.agent_response = None
                return state

            except Exception as e:
                logger.error(f"Erro ao processar endereço: {e}")
                validation_state.last_error = str(e)
                state.data["address_validation"] = validation_state.model_dump()

                if validation_state.attempts >= validation_state.max_attempts:
                    state.agent_response = AgentResponse(
                        description=self.templates.endereco_maximo_tentativas(),
                        error_message="Máximo de tentativas excedido",
                    )
                    state.data["address_max_attempts_reached"] = True
                else:
                    state.agent_response = AgentResponse(
                        description=self.templates.endereco_erro_processamento(
                            validation_state.attempts, validation_state.max_attempts
                        ),
                        payload_schema=AddressPayload.model_json_schema(),
                        error_message=f"Erro: {str(e)}",
                    )
                return state

        if self.common_config.address_required:
            state.agent_response = AgentResponse(
                description=self.templates.solicitar_endereco(),
                payload_schema=AddressPayload.model_json_schema(),
            )

        return state

    @handle_errors
    async def _collect_reference_point(self, state: ServiceState) -> ServiceState:
        logger.info("[ENTRADA] _collect_reference_point")

        if state.payload and "correcao" in state.payload:
            return state

        if state.data.get("correction_requested") == "reference_point":
            state.data.pop("correction_requested", None)
            # Correção explícita: o cidadão quer informar/alterar o ponto de
            # referência mesmo que o serviço não o exija (reference_point_required
            # = False). Reativa a coleta — senão o early-return abaixo descartaria
            # a entrada explícita junto com a pergunta forçada que removemos.
            state.data["need_reference_point"] = True
        elif state.data.get("correction_requested"):
            return state

        if state.data.get("reference_point_collected") or not state.data.get(
            "need_reference_point"
        ):
            return state

        if (
            state.payload
            and "ponto_referencia" not in state.payload
            and state.agent_response is None
        ):
            state.agent_response = AgentResponse(
                description=self.templates.solicitar_ponto_referencia(),
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

                if (
                    state.data.get("cpf")
                    or state.data.get("email")
                    or state.data.get("name")
                ):
                    state.data["personal_data_needs_confirmation"] = True

                state.agent_response = None
                return state

            except Exception as e:
                logger.warning(f"Erro ao processar ponto de referência: {e}")
                state.data["ponto_referencia"] = None
                state.data["reference_point_collected"] = True

                if (
                    state.data.get("cpf")
                    or state.data.get("email")
                    or state.data.get("name")
                ):
                    state.data["personal_data_needs_confirmation"] = True

                state.agent_response = None
                return state

        if self.common_config.reference_point_required or state.data.get(
            "need_reference_point"
        ):
            state.agent_response = AgentResponse(
                description=self.templates.solicitar_ponto_referencia(),
                payload_schema=PontoReferenciaPayload.model_json_schema(),
            )

        return state

    @handle_errors
    async def _confirm_address(self, state: ServiceState) -> ServiceState:
        logger.info("[ENTRADA] _confirm_address")

        if state.data.get("address_confirmed"):
            return state

        if not state.data.get("address_needs_confirmation"):
            logger.warning(
                "_confirm_address chamado mas não há endereço para confirmar"
            )
            return state

        if "confirmacao" in state.payload:
            try:
                validated_data = AddressConfirmationPayload.model_validate(
                    state.payload
                )

                if validated_data.confirmacao:
                    state.data["address"] = state.data["address_temp"]
                    state.data["address_confirmed"] = True
                    state.data["address_validated"] = True
                    state.data["address_needs_confirmation"] = False
                    # Sempre solicita ponto de referência (mesmo quando opcional)
                    # para melhorar precisão da localização
                    state.data["need_reference_point"] = True

                    logger.info("Endereço confirmado pelo usuário")
                    state.agent_response = None
                    return state

                state.data["address_needs_confirmation"] = False
                state.data["address_temp"] = None
                state.data["address_validated"] = False
                logger.info("Endereço não confirmado, solicitando novo endereço")

                validation_state = AddressValidationState(
                    **state.data.get("address_validation", {})
                )

                if validation_state.attempts >= validation_state.max_attempts:
                    state.agent_response = AgentResponse(
                        description=self.templates.endereco_maximo_tentativas(),
                        error_message="Máximo de tentativas excedido",
                    )
                    state.data["address_max_attempts_reached"] = True
                else:
                    state.agent_response = AgentResponse(
                        description=self.templates.solicitar_novo_endereco(
                            validation_state.attempts, validation_state.max_attempts
                        ),
                        payload_schema=AddressPayload.model_json_schema(),
                    )
                return state

            except Exception as e:
                state.agent_response = AgentResponse(
                    description=self.templates.confirmar_resposta_invalida(),
                    payload_schema=AddressConfirmationPayload.model_json_schema(),
                    error_message=f"Resposta inválida: {str(e)}",
                )
                return state

        if state.data.get("address_temp"):
            address_temp = state.data.get("address_temp", {})
            msg_confirmacao = self.format_address_confirmation(address_temp)

            if not state.payload and state.data.get("address_temp"):
                state.agent_response = AgentResponse(
                    description=self.templates.endereco_historico(msg_confirmacao),
                    payload_schema=AddressConfirmationPayload.model_json_schema(),
                )
            else:
                state.agent_response = AgentResponse(
                    description=self.templates.confirmar_endereco(msg_confirmacao),
                    payload_schema=AddressConfirmationPayload.model_json_schema(),
                )

        return state
