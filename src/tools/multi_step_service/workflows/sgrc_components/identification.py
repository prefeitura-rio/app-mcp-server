from loguru import logger

from src.tools.multi_step_service.core.base_workflow import handle_errors
from src.tools.multi_step_service.core.models import AgentResponse, ServiceState
from src.tools.multi_step_service.workflows.sgrc_components.models import (
    AddressConfirmationPayload,
    CPFPayload,
    EmailPayload,
    NomePayload,
)
from src.tools.multi_step_service.workflows.sgrc_components import templates as tpl


class IdentificationFlowMixin:
    def increment_attempts(self, state: ServiceState, key: str) -> int:
        attempts = state.data.get(key, 0) + 1
        state.data[key] = attempts
        return attempts

    def _identification_required(self) -> bool:
        return bool(getattr(self.common_config, "identification_required", False))

    def _personal_data_template(self, name: str, *args):
        template_fn = getattr(tpl, name)
        required_aware = {
            "solicitar_cpf",
            "cpf_invalido",
            "maximo_tentativas_excedido",
            "solicitar_email",
            "email_invalido",
            "email_maximo_tentativas",
            "solicitar_nome",
            "nome_invalido",
            "nome_maximo_tentativas",
        }
        if name in required_aware:
            return template_fn(*args, required=self._identification_required())
        return template_fn(*args)

    @handle_errors
    async def _collect_cpf(self, state: ServiceState) -> ServiceState:
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
                        logger.info(
                            "[MEMÓRIA] Usuário confirmou usar dados pessoais anteriores"
                        )
                        state.data["cadastro_verificado"] = True
                        state.agent_response = None
                        return state

                    logger.info(
                        "[MEMÓRIA] Usuário recusou dados pessoais anteriores. Limpando..."
                    )
                    for key in [
                        "cpf",
                        "email",
                        "name",
                        "phone",
                        "cadastro_verificado",
                        "identificacao_pulada",
                        "cpf_attempts",
                        "email_attempts",
                        "name_attempts",
                        "awaiting_user_memory_confirmation",
                    ]:
                        state.data.pop(key, None)

                    state.agent_response = AgentResponse(
                        description=self._personal_data_template("solicitar_cpf"),
                        payload_schema=CPFPayload.model_json_schema(),
                    )
                    return state

                except Exception as e:
                    logger.error(
                        f"Erro ao processar confirmação de dados pessoais: {e}"
                    )
            else:
                user_input = (
                    str(state.payload.get("email", "")).lower()
                    if "email" in state.payload
                    else ""
                )

                if user_input == "" and "email" in state.payload:
                    logger.info(
                        "[MEMÓRIA] Usuário optou por pular dados pessoais (avançar)"
                    )
                    state.data.pop("awaiting_user_memory_confirmation", None)

                    for key in ["cpf", "email", "name", "phone", "cadastro_verificado"]:
                        state.data.pop(key, None)

                    state.agent_response = None
                    return state
                logger.info(
                    "[MEMÓRIA] Usuário não enviou confirmação - repetindo pergunta"
                )

        payload_is_empty = not state.payload or (
            len(state.payload) == 1 and "ponto_referencia" in state.payload
        )

        should_ask_about_data = (
            state.data.get("address_confirmed")
            or state.data.get("personal_data_needs_confirmation")
            or (
                state.data.get("identificacao_pulada")
                and not state.data.get("name")
                and not state.data.get("email")
            )
        )

        if (
            (
                payload_is_empty
                and not state.data.get("awaiting_user_memory_confirmation")
            )
            or (
                state.data.get("awaiting_user_memory_confirmation")
                and "confirmacao" not in state.payload
            )
        ) and should_ask_about_data:
            masked_data = []

            if state.data.get("name"):
                parts = state.data["name"].split()
                nome_mask = (
                    f"{parts[0]} {parts[-1][0]}." if len(parts) > 1 else parts[0]
                )
                masked_data.append(f"- Nome: {nome_mask}")

            if state.data.get("cpf"):
                cpf = state.data["cpf"]
                cpf_mask = f"XXX.{cpf[3:6]}.{cpf[6:9]}-XX" if len(cpf) == 11 else "XXX"
                masked_data.append(f"- CPF: {cpf_mask}")

            if state.data.get("email"):
                user, _, domain = state.data["email"].partition("@")
                email_mask = (
                    f"{user[:2]}***@{domain}"
                    if len(user) > 2
                    else f"{user}***@{domain}"
                )
                masked_data.append(f"- Email: {email_mask}")

            if masked_data:
                message = (
                    "Por questões de segurança, não posso exibir dados sensíveis completos.\n\n"
                    if state.data.get("awaiting_user_memory_confirmation")
                    and state.payload
                    else ""
                )
                message += self._personal_data_template(
                    "confirmar_dados_salvos", masked_data
                )
                state.data["awaiting_user_memory_confirmation"] = True
                state.data.pop("personal_data_needs_confirmation", None)

                state.agent_response = AgentResponse(
                    description=message,
                    payload_schema=AddressConfirmationPayload.model_json_schema(),
                )
                return state

        if state.payload and "cpf" in state.payload:
            try:
                validated = CPFPayload.model_validate(state.payload)
                cpf_novo = validated.cpf

                if not cpf_novo:
                    if self.common_config.identification_required:
                        raise ValueError("CPF é obrigatório para este serviço")

                    state.data["cpf"] = ""
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
                            state.data["phone"] = (
                                str(user_info["phones"][0]).strip()
                                if user_info["phones"][0]
                                else ""
                            )

                        state.data["cadastro_verificado"] = True
                        logger.info("Cadastro encontrado")

                    except AttributeError as e:
                        logger.error(
                            f"Erro ao processar resposta da API de cadastro: {str(e)}"
                        )
                        state.data["cadastro_verificado"] = False
                    except (ConnectionError, TimeoutError) as e:
                        logger.error(f"API de cadastro indisponível: {str(e)}")
                        state.data["cadastro_verificado"] = False
                        state.data["api_indisponivel"] = True
                    except Exception as e:
                        logger.info(
                            f"Usuário não encontrado no cadastro ou erro na consulta: {str(e)}"
                        )
                        state.data["cadastro_verificado"] = False

                state.agent_response = None
                return state

            except Exception as e:
                attempts = self.increment_attempts(state, "cpf_attempts")

                if attempts >= 3:
                    if self.common_config.identification_required:
                        state.data["cpf_max_attempts_reached"] = True
                        state.agent_response = AgentResponse(
                            description=self._personal_data_template(
                                "maximo_tentativas_excedido"
                            ),
                            error_message="Máximo de tentativas excedido",
                        )
                        return state

                    state.data["cpf"] = ""
                    state.data["identificacao_pulada"] = True
                    state.data["cpf_max_attempts_reached"] = True
                    state.agent_response = AgentResponse(
                        description=self._personal_data_template(
                            "maximo_tentativas_excedido"
                        ),
                        error_message="Máximo de tentativas excedido",
                    )
                    return state

                state.agent_response = AgentResponse(
                    description=self._personal_data_template("cpf_invalido", attempts),
                    payload_schema=CPFPayload.model_json_schema(),
                    error_message=str(e),
                )

        if "cpf" in state.data and "cpf" not in state.payload:
            if (
                state.data.get("identificacao_pulada")
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
                description=self._personal_data_template("solicitar_cpf"),
                payload_schema=CPFPayload.model_json_schema(),
            )

        return state

    @handle_errors
    async def _collect_email(self, state: ServiceState) -> ServiceState:
        logger.info("[ENTRADA] _collect_email")

        if state.data.get("correction_requested") == "email":
            state.data.pop("correction_requested", None)
        elif state.data.get("email_processed"):
            return state

        if state.payload and "email" in state.payload:
            email_value = state.payload.get("email")
            if not email_value or (
                isinstance(email_value, str) and email_value.strip() == ""
            ):
                state.data["email_skipped"] = True
                state.data["email_processed"] = True
                logger.info("Usuário optou por não informar email")
                state.agent_response = None
                return state

            try:
                validated_data = EmailPayload.model_validate(state.payload)
                state.data["email"] = validated_data.email
                state.data["email_processed"] = True
                logger.info(f"Email coletado: {validated_data.email}")
                state.agent_response = None
                return state

            except Exception as e:
                attempts = self.increment_attempts(state, "email_attempts")
                logger.info(f"[EMAIL] Tentativa {attempts}/3 - Erro: {str(e)}")

                if attempts >= 3:
                    state.data["email_skipped"] = True
                    state.data["email_processed"] = True
                    state.data["email_max_attempts_reached"] = True
                    state.agent_response = AgentResponse(
                        description=self._personal_data_template(
                            "email_maximo_tentativas"
                        ),
                        error_message="Máximo de tentativas excedido",
                    )
                    return state

                state.agent_response = AgentResponse(
                    description=self._personal_data_template(
                        "email_invalido", attempts
                    ),
                    payload_schema=EmailPayload.model_json_schema(),
                    error_message=str(e),
                )
                return state

        if state.data.get("email_processed") and "email" not in state.payload:
            return state

        if state.agent_response and state.agent_response.error_message:
            return state

        if not state.data.get("email_processed"):
            state.agent_response = AgentResponse(
                description=self._personal_data_template("solicitar_email"),
                payload_schema=EmailPayload.model_json_schema(),
            )

        return state

    @handle_errors
    async def _collect_name(self, state: ServiceState) -> ServiceState:
        logger.info("[ENTRADA] _collect_name")

        if state.data.get("correction_requested") == "name":
            state.data.pop("correction_requested", None)
        elif state.data.get("name_processed"):
            return state

        if state.payload and "name" in state.payload:
            name_value = state.payload.get("name")
            if not name_value or (
                isinstance(name_value, str) and name_value.strip() == ""
            ):
                state.data["name_skipped"] = True
                state.data["name_processed"] = True
                logger.info("Usuário optou por não informar nome")
                state.agent_response = None
                return state

            try:
                validated_data = NomePayload.model_validate(state.payload)
                state.data["name"] = validated_data.name
                state.data["name_processed"] = True
                logger.info(f"Nome coletado: {validated_data.name}")
                state.agent_response = None
                return state

            except Exception as e:
                attempts = self.increment_attempts(state, "name_attempts")

                if attempts >= 3:
                    state.data["name_skipped"] = True
                    state.data["name_processed"] = True
                    state.data["name_max_attempts_reached"] = True
                    state.agent_response = AgentResponse(
                        description=self._personal_data_template(
                            "nome_maximo_tentativas"
                        ),
                        error_message="Máximo de tentativas excedido",
                    )
                    return state

                state.agent_response = AgentResponse(
                    description=self._personal_data_template("nome_invalido", attempts),
                    payload_schema=NomePayload.model_json_schema(),
                    error_message=str(e),
                )
                return state

        if state.data.get("name_processed") and "name" not in state.payload:
            return state

        if state.agent_response and state.agent_response.error_message:
            return state

        if not state.data.get("name_processed"):
            state.agent_response = AgentResponse(
                description=self._personal_data_template("solicitar_nome"),
                payload_schema=NomePayload.model_json_schema(),
            )

        return state
