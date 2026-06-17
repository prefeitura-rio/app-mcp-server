import unicodedata

from loguru import logger

from src.tools.auth.govbr_auth import govbr_auth_init, govbr_auth_status
from src.tools.multi_step_service.core.base_workflow import handle_errors
from src.tools.multi_step_service.core.models import AgentResponse, ServiceState
from src.tools.multi_step_service.workflows.sgrc_components.models import (
    AddressConfirmationPayload,
    CPFPayload,
    EmailPayload,
    IdentificationMethodPayload,
    NomePayload,
)
from src.tools.multi_step_service.workflows.sgrc_components import templates as tpl


# Termos de RECUSA de identificação — só valem quando a identificação é OPCIONAL.
# Match exato cobre os tokens curtos; o substring cobre as frases naturais que o
# agente repassa. Compartilhado entre a escolha de método (_select_identification_method)
# e o abort do gov.br no estado de espera (_authenticate_govbr), pra a recusa ser
# reconhecida de forma consistente nos dois pontos.
_SKIP_TOKENS = {
    "anonimo",
    "anônimo",
    "anonima",
    "anônima",
    "pular",
    "skip",
    "nao",
    "não",
    "nenhum",
    "nenhuma",
    "recusar",
    "recuso",
    "nao quero",
    "não quero",
    "nao quero me identificar",
    "não quero me identificar",
    "sem identificacao",
    "sem identificação",
    "seguir sem",
    "continuar sem",
}
_SKIP_SUBSTRINGS = (
    "sem ident",
    "sem me ident",
    "nao me ident",
    "não me ident",
    "sem se ident",
    "continuar sem",
    "seguir sem",
    "nao quero",
    "não quero",
    "anonim",
    "pular",
    "recus",
    "nenhum",
    "sem cpf",
)
# Recusa INEQUÍVOCA de identificação no estado de espera do gov.br. Em
# _authenticate_govbr ela é avaliada DEPOIS de retry e CPF e só quando a
# identificação é opcional — então frases que citam "tentar"/"cpf" (e suas negações,
# p.ex. "não desisti, quero tentar novamente" / "não tenho cpf, quero tentar de
# novo") já foram roteadas pro reenvio/CPF e NÃO chegam aqui (codex P2). Por isso o
# set é enxuto: sem termos facilmente negáveis (desist/cancelar) e sem termos que
# colidam com pedido de cpf. Casado contra texto normalizado (sem acento).
_GOVBR_ABORT_SUBSTRINGS = (
    "nao quero me identificar",
    "nao quero identificar",
    "nao quero me ident",
    "nao me identificar",
    "sem me identificar",
    "sem me ident",
    "sem identificar",
    "sem identificacao",
    "sem ident",
    "anonim",
    # Tokens de skip que o _select_identification_method também aceita — incluídos pra
    # a recusa ser reconhecida de forma consistente nos dois pontos (codex P2). NÃO
    # incluir "nenhum": colide com a queixa de entrega "não recebi nenhum link", que
    # é pedido de reenvio (tratado pela pista "nao recebi" no ramo de retry), não recusa.
    "pular",
    "skip",
    "recus",
    "seguir sem",
    "continuar sem",
)
# Recusas que MENCIONAM cpf pra RECUSAR ("sem cpf", "não quero usar cpf"). Roteadas
# pro abort/anônimo (e excluídas do ramo CPF). Afirmativas ("quero usar cpf") não
# entram aqui (codex P2).
_CPF_REFUSAL_SUBSTRINGS = (
    "sem cpf",
    "sem o cpf",
    "sem usar cpf",
    "nao quero cpf",
    "nao quero o cpf",
    "nao quero usar cpf",
    "nao quero usar o cpf",
    "nao usar cpf",
    "nao vou usar cpf",
)
# Menções de cpf que NÃO são pedido afirmativo: as recusas acima + "não tenho cpf" +
# recusa de identificação que cita cpf. Servem só pra EXCLUIR do ramo CPF (codex P2):
# "não tenho cpf, quero tentar novamente" segue pro reenvio (não é recusa de
# identificação), e "não quero me identificar com cpf" cai na recusa (≠ "com gov.br,
# quero usar cpf", que troca pra CPF).
_CPF_NOT_AFFIRMATIVE = _CPF_REFUSAL_SUBSTRINGS + (
    "nao tenho cpf",
    "nao tenho o cpf",
    "nao quero me identificar com cpf",
    "nao quero me identificar com o cpf",
)


def _normalize_text(value: str) -> str:
    """lower + strip de acentos pra casar tokens de recusa de forma robusta
    ("anônimo"/"ANÔNIMO"/"anonimo" caem todos no mesmo token)."""
    text = (value or "").strip().lower()
    return "".join(
        ch
        for ch in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(ch)
    )


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
        # solicitar_metodo_identificacao E metodo_identificacao_invalido precisam
        # saber se é opcional pra MANTER a opção de pular visível — inclusive no
        # re-prompt de erro (senão o cidadão anônimo fica preso pedindo CPF/Gov.br;
        # incidente 2026-06-04). args é () pro solicitar e (tentativa,) pro inválido.
        if name in ("solicitar_metodo_identificacao", "metodo_identificacao_invalido"):
            identificacao_obrigatoria = getattr(
                self.common_config, "identification_required", False
            )
            return template_fn(*args, opcional=not identificacao_obrigatoria)

        simple_templates = {
            "govbr_autenticacao_iniciada",
            "govbr_autenticacao_pendente",
            "govbr_autenticacao_erro",
            "govbr_dados_coletados",
            "confirmar_dados_salvos",
        }

        if name in simple_templates:
            return template_fn(*args)
        if name in required_aware:
            return template_fn(*args, required=self._identification_required())
        return template_fn(*args)

    @handle_errors
    async def _select_identification_method(self, state: ServiceState) -> ServiceState:
        """
        First step in identification: let user choose CPF or Gov.br.

        Sets state.data['identification_method'] to 'cpf' or 'govbr'.
        """
        logger.info("[ENTRADA] _select_identification_method")

        if state.data.get("identification_method"):
            logger.info(
                f"[METHOD] Already selected: {state.data['identification_method']}"
            )
            return state

        if state.data.get("cpf") or state.data.get("govbr_authenticated"):
            logger.info("[METHOD] User already identified, skipping method selection")
            return state

        # Se identificação NÃO é obrigatória E payload vazio → usuário quer pular
        identificacao_obrigatoria = state.data.get(
            "identificacao_obrigatoria_1746", False
        )
        payload_vazio = not state.payload or len(state.payload) == 0

        # Botões do método (camada-tool, gated ENABLE_INTERACTIVE_CONFIRM). Os títulos
        # mapeiam pros valores do campo: "CPF"→cpf / "Gov.br"→govbr (normalize_method);
        # "Sem me identificar" cai no skip-substring → anônimo, antes da validação. A
        # 3ª opção só aparece quando a identificação é opcional.
        metodo_buttons = [
            {"id": "cpf", "title": "CPF"},
            {"id": "govbr", "title": "Gov.br"},
        ]
        if not identificacao_obrigatoria:
            metodo_buttons.append({"id": "anonimo", "title": "Sem me identificar"})

        if not identificacao_obrigatoria and payload_vazio:
            logger.info(
                "[METHOD] Identificação opcional + payload vazio → pulando (anônimo)"
            )
            state.data["identification_method"] = "anonimo"
            state.data["identificacao_recusada"] = True
            state.agent_response = None
            return state

        # Identificação opcional: além do payload vazio, reconhecer a recusa
        # EXPLÍCITA vinda no payload. O agente costuma mandar "anonimo"/"pular"/
        # "nao" quando o cidadão diz que não quer se identificar; sem isto, a
        # validação estrita cpf/govbr falhava 3x e caía em CPF, prendendo o
        # cidadão num loop confuso (achado 2026-06-03). Só vale quando opcional.
        if not identificacao_obrigatoria:
            metodo_raw = (
                str(state.payload.get("identification_method", "")).strip().lower()
            )
            # Recusa explícita → anônimo. Match exato cobre tokens curtos; substring
            # cobre as frases naturais que o agente repassa ("quero continuar sem me
            # identificar", "seguir sem cpf"...). Sem o substring, o exato falhava
            # nelas e o cidadão caía na validação estrita cpf/govbr → erro "escolha CPF
            # ou Gov.br", contradizendo o "é opcional" (achado 2026-06-04). Como a
            # identificação é OPCIONAL aqui, interpretar recusa de forma liberal é
            # seguro: input claro de cpf/govbr é normalizado antes de chegar aqui.
            if metodo_raw in _SKIP_TOKENS or any(
                s in metodo_raw for s in _SKIP_SUBSTRINGS
            ):
                logger.info(
                    "[METHOD] Identificação opcional + recusa explícita → anônimo"
                )
                state.data["identification_method"] = "anonimo"
                state.data["identificacao_recusada"] = True
                state.agent_response = None
                return state

        if state.payload and "identification_method" in state.payload:
            try:
                validated = IdentificationMethodPayload.model_validate(state.payload)
                state.data["identification_method"] = validated.identification_method
                logger.info(f"[METHOD] User chose: {validated.identification_method}")
                state.agent_response = None
                return state

            except Exception as e:
                attempts = self.increment_attempts(state, "method_attempts")
                logger.info(f"[METHOD] Invalid choice, attempt {attempts}/3")

                if attempts >= 3:
                    logger.info("[METHOD] Max attempts reached, defaulting to CPF")
                    state.data["identification_method"] = "cpf"
                    state.agent_response = None
                    return state

                desc_invalido = self._personal_data_template(
                    "metodo_identificacao_invalido", attempts
                )
                state.agent_response = AgentResponse(
                    description=desc_invalido,
                    payload_schema=IdentificationMethodPayload.model_json_schema(),
                    interactive={
                        "body": desc_invalido,
                        "field": "identification_method",
                        "buttons": metodo_buttons,
                    },
                    error_message=str(e),
                )
                return state

        desc_metodo = self._personal_data_template("solicitar_metodo_identificacao")
        state.agent_response = AgentResponse(
            description=desc_metodo,
            payload_schema=IdentificationMethodPayload.model_json_schema(),
            interactive={
                "body": desc_metodo,
                "field": "identification_method",
                "buttons": metodo_buttons,
            },
        )
        return state

    @handle_errors
    async def _authenticate_govbr(self, state: ServiceState) -> ServiceState:
        """
        Handle Gov.br OAuth authentication flow.

        Steps:
        1. Check if already authenticated
        2. If not, call govbr_auth_init to send auth button
        3. Wait for user to complete authentication
        4. Extract user_info (cpf, nome, email) from token
        5. Call internal API with gov.br CPF to get phone number
        6. Merge data: gov.br data + internal API phone
        """
        logger.info("[ENTRADA] _authenticate_govbr")

        if state.data.get("identification_method") != "govbr":
            logger.info("[GOVBR] Not using gov.br method, skipping")
            return state

        if state.data.get("govbr_authenticated"):
            logger.info("[GOVBR] Already authenticated via gov.br")
            return state

        user_number = f"+{state.user_id}"

        status = await govbr_auth_status(user_number)

        if status.get("is_authenticated"):
            logger.info("[GOVBR] User is authenticated, extracting data")

            user_info = status.get("user_info", {})

            govbr_cpf = user_info.get("cpf")
            govbr_nome = user_info.get("nome")
            govbr_email = user_info.get("email")

            logger.info(f"[GOVBR] Raw user_info from token: {user_info}")
            logger.info(
                f"[GOVBR] Extracted - CPF: {govbr_cpf}, Nome: {govbr_nome}, Email: {govbr_email}"
            )

            if not govbr_cpf:
                logger.error("[GOVBR] Token missing CPF - cannot proceed")
                state.agent_response = AgentResponse(
                    description=self._personal_data_template("govbr_autenticacao_erro"),
                )
                state.data.pop("identification_method", None)
                return state

            logger.info(f"[GOVBR] Got CPF from token: {govbr_cpf}")

            state.data["cpf"] = govbr_cpf
            if govbr_nome:
                state.data["name"] = govbr_nome
            if govbr_email:
                state.data["email"] = govbr_email

            if not self.use_fake_api:
                try:
                    logger.info("[GOVBR] Fetching additional data from internal API")
                    user_info_api = await self.api_service.get_user_info(govbr_cpf)

                    if user_info_api.get("phones"):
                        state.data["phone"] = (
                            str(user_info_api["phones"][0]).strip()
                            if user_info_api["phones"][0]
                            else ""
                        )
                        logger.info("[GOVBR] Got phone from internal API")

                    if not govbr_email and user_info_api.get("email"):
                        state.data["email"] = user_info_api["email"].strip().lower()
                    if not govbr_nome and user_info_api.get("name"):
                        state.data["name"] = user_info_api["name"].strip()

                    state.data["cadastro_verificado"] = True

                except Exception as e:
                    logger.info(
                        f"[GOVBR] Internal API lookup failed (non-critical): {e}"
                    )
                    state.data["cadastro_verificado"] = False

            state.data["govbr_authenticated"] = True

            nome_display = (
                state.data.get("name", "").split()[0]
                if state.data.get("name")
                else "usuário"
            )
            state.agent_response = AgentResponse(
                description=self._personal_data_template(
                    "govbr_dados_coletados", nome_display
                ),
            )

            return state

        if state.data.get("govbr_auth_sent"):
            logger.info("[GOVBR] Auth link already sent, waiting for user")

            # Texto livre vem em `message` (às vezes em `identification_method`).
            # Normaliza (lower + sem acento via _normalize_text) pra casar recusa de
            # forma robusta: "anônimo"/"ANÔNIMO"/"anonimo" caem no mesmo token.
            user_input = _normalize_text(
                " ".join(
                    str(state.payload.get(k, ""))
                    for k in ("message", "identification_method")
                )
                if state.payload
                else ""
            )

            identificacao_obrigatoria = state.data.get(
                "identificacao_obrigatoria_1746", False
            )

            # Desambiguação por substring tem limite intrínseco: NÃO distingue "não
            # quero me identificar" (recusa) de "não recusei"/"não quero pular" (retry
            # negado). Escolhemos REENVIO-PRIMEIRO (a recomendação mais recente do codex)
            # porque é o mais SEGURO: o pior caso é mandar um link a mais, nunca um
            # opt-out silencioso de quem ainda quer continuar.
            #   1) REENVIO: qualquer sinal de retry/entrega ("tentar", "novamente",
            #      "novo link", "não recebi"). Captura as frases de retry-com-negação
            #      ("não recusei, quero tentar novamente", "não quero pular, manda novo
            #      link") ANTES da recusa, evitando opt-out indevido (codex P2).
            #   2) CPF AFIRMATIVO: cita "cpf" e não é menção não-afirmativa ("sem cpf",
            #      "não tenho cpf", "não quero me identificar com cpf").
            #   3) RECUSA de identificação (inclui recusas que citam cpf) → anônimo.
            #   4) Pendente.
            # Limitação conhecida (não-aprisionante): "não recebi o link e não quero me
            # identificar" reenvia em vez de seguir anônimo — o cidadão repete a recusa
            # sozinha. O fix robusto é trocar este estado por BOTÕES (depende do #1/Mule),
            # eliminando a classificação de texto livre.
            if (
                "tentar" in user_input
                or "novamente" in user_input
                or "novo link" in user_input
                or "nao recebi" in user_input
            ):
                logger.info("[GOVBR] User requested new auth link")
                state.data.pop("govbr_auth_sent", None)
            elif "cpf" in user_input and not any(
                x in user_input for x in _CPF_NOT_AFFIRMATIVE
            ):
                logger.info("[GOVBR] User wants to switch to CPF method")
                state.data["identification_method"] = "cpf"
                state.data.pop("govbr_auth_sent", None)
                state.agent_response = None
                return state
            elif not identificacao_obrigatoria and (
                any(s in user_input for s in _GOVBR_ABORT_SUBSTRINGS)
                or any(r in user_input for r in _CPF_REFUSAL_SUBSTRINGS)
            ):
                # Recusa de identificação → anônimo, em vez de prender no loop
                # "aguardando autenticação" (device-test 2026-06-17). `identificacao_pulada`
                # faz _collect_cpf/_route_after_cpf pularem pra confirmação nos dois
                # workflows (a rota _route_after_govbr_auth cai em collect_cpf, que então
                # pula); limpa o estado do gov.br.
                logger.info("[GOVBR] User aborted gov.br → anônimo")
                state.data["identification_method"] = "anonimo"
                state.data["identificacao_recusada"] = True
                state.data["identificacao_pulada"] = True
                state.data.pop("govbr_auth_sent", None)
                state.data.pop("govbr_auth_id", None)
                state.agent_response = None
                return state
            else:
                state.agent_response = AgentResponse(
                    description=self._personal_data_template(
                        "govbr_autenticacao_pendente"
                    ),
                )
                return state

        if not state.data.get("govbr_auth_sent"):
            logger.info("[GOVBR] Initiating auth flow")

            service_context = getattr(self, "service_name", "multi_step_service")

            init_result = await govbr_auth_init(
                user_number=user_number,
                service_context=service_context,
            )

            if init_result.get("status") != "ok":
                logger.error(f"[GOVBR] Auth init failed: {init_result.get('error')}")
                state.agent_response = AgentResponse(
                    description=self._personal_data_template("govbr_autenticacao_erro"),
                )
                state.data.pop("identification_method", None)
                return state

            state.data["govbr_auth_sent"] = True
            state.data["govbr_auth_id"] = init_result.get("auth_id")

            logger.info(
                f"[GOVBR] Auth link sent successfully, auth_id={init_result.get('auth_id')}"
            )

            state.agent_response = AgentResponse(
                description=self._personal_data_template("govbr_autenticacao_iniciada"),
            )

            return state

        return state

    @handle_errors
    async def _collect_cpf(self, state: ServiceState) -> ServiceState:
        logger.info("[ENTRADA] _collect_cpf")

        if state.data.get("identification_method") == "govbr":
            logger.info("[CPF] Using gov.br method, skipping CPF collection")
            return state

        if state.data.get("govbr_authenticated"):
            logger.info("[CPF] Already authenticated via gov.br")
            return state

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
