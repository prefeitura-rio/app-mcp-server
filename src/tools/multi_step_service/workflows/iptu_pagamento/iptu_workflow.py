"""
Workflow IPTU Ano Vigente - Prefeitura do Rio de Janeiro

Implementa o fluxo completo de consulta e emissão de guias de IPTU
seguindo o fluxograma oficial da Prefeitura do Rio.
"""

import os
from langgraph.graph import StateGraph, END
from loguru import logger

from src.tools.multi_step_service.core.base_workflow import BaseWorkflow, handle_errors
from src.tools.multi_step_service.core.models import ServiceState, AgentResponse

from src.tools.multi_step_service.workflows.iptu_pagamento.core.models import (
    InscricaoImobiliariaPayload,
    EscolhaAnoPayload,
    EscolhaGuiasIPTUPayload,
    EscolhaCotasParceladasPayload,
    EscolhaMaisCotasPayload,
    EscolhaOutrasGuiasPayload,
    EscolhaOutroImovelPayload,
    EscolhaFormatoDarmPayload,
    ConfirmacaoDadosPayload,
    DadosCotas,
)
from src.tools.multi_step_service.workflows.iptu_pagamento.api.api_service import (
    IPTUAPIService,
)
from src.tools.multi_step_service.workflows.iptu_pagamento.api.api_service_fake import (
    IPTUAPIServiceFake,
)
from src.tools.multi_step_service.workflows.iptu_pagamento.api.exceptions import (
    APIUnavailableError,
    AuthenticationError,
)
from src.tools.multi_step_service.workflows.iptu_pagamento.templates import (
    IPTUMessageTemplates,
)
from src.tools.multi_step_service.workflows.iptu_pagamento.helpers import utils
from src.tools.multi_step_service.workflows.iptu_pagamento.helpers import state_helpers
from src.tools.multi_step_service.workflows.iptu_pagamento.core.constants import (
    FAKE_API_ENV_VAR,
    MAX_TENTATIVAS_ANO,
    ERROR_INSCRICAO_AUSENTE,
    ERROR_ANO_AUSENTE,
    STATE_IS_DATA_CONFIRMED,
    STATE_WANTS_MORE_QUOTAS,
    STATE_WANTS_OTHER_GUIAS,
    STATE_WANTS_OTHER_PROPERTY,
    STATE_HAS_CONSULTED_GUIAS,
    STATE_USE_SEPARATE_DARM,
    STATE_NEXT_QUESTION_TYPE,
    QUESTION_TYPE_MORE_QUOTAS,
    QUESTION_TYPE_OTHER_GUIAS,
    QUESTION_TYPE_OTHER_PROPERTY,
    STATE_FAILED_ATTEMPTS_PREFIX,
)


class IPTUWorkflow(BaseWorkflow):
    """
    Workflow para consulta de IPTU da Prefeitura do Rio.

    Fluxo principal adaptado:
    1. Informar inscrição imobiliária
    2. Escolher ano de exercício (2024, 2025, 2026)
    3. Consultar guias de IPTU disponíveis para pagamento
    4. Escolher quais guias de IPTU quer pagar (múltipla seleção)
    5. Verifica se é cota única (informa forma de pagamento)
    6. Se parcelado: escolher cotas a pagar
    7. Deseja pagar as cotas em DARF separado?
    8. Confirmação dos dados coletados para pagamento
    9. Existe mais guia a pagar nesse imóvel?
    10. Deseja emitir guias para outro imóvel?
    """

    service_name = "iptu_pagamento"
    description = "Consulta e emissão de guias de IPTU - Prefeitura do Rio de Janeiro."

    def __init__(self, use_fake_api: bool = False):
        """
        Inicializa o workflow IPTU.

        Args:
            use_fake_api: Se True, usa api_service_fake com dados mockados.
                         Se False, usa api_service real.
        """
        super().__init__()

        # Verifica variável de ambiente para testes
        force_fake_api = os.getenv(FAKE_API_ENV_VAR, "").lower() == "true"

        if use_fake_api or force_fake_api:
            self.api_service = IPTUAPIServiceFake()
        else:
            self.api_service = IPTUAPIService()

    # --- Nós do Grafo ---

    @handle_errors
    async def _informar_inscricao_imobiliaria(
        self, state: ServiceState
    ) -> ServiceState:
        """Coleta a inscrição imobiliária do usuário."""
        # Verifica se há uma nova inscrição no payload (diferente da atual)
        if "inscricao_imobiliaria" in state.payload:
            try:
                validated_data = InscricaoImobiliariaPayload.model_validate(
                    state.payload
                )
                nova_inscricao = validated_data.inscricao_imobiliaria
                inscricao_atual = state.data.get("inscricao_imobiliaria")

                # Se é uma nova inscrição diferente da atual, faz reset automático
                if inscricao_atual and nova_inscricao != inscricao_atual:
                    # Reset automático para nova inscrição
                    state_helpers.reset_completo(state)

                # Salva a nova inscrição
                state.data["inscricao_imobiliaria"] = nova_inscricao
                logger.info(f"✅ Inscrição salva: {nova_inscricao}")

                logger.debug(
                    f"🔍 Buscando dados do imóvel para inscrição: {nova_inscricao}"
                )
                try:
                    dados_imovel = await self.api_service.get_imovel_info(
                        inscricao=nova_inscricao
                    )

                    if dados_imovel:
                        state.data["endereco"] = dados_imovel["endereco"]
                        state.data["proprietario"] = dados_imovel["proprietario"]
                        logger.info(
                            f"✅ Dados do imóvel carregados - Proprietário: {dados_imovel['proprietario'][:30]}..."
                        )
                except (APIUnavailableError, AuthenticationError) as e:
                    # Se falhar ao buscar dados do imóvel, continua sem eles
                    logger.warning(
                        f"Não foi possível carregar dados do imóvel: {str(e)}"
                    )
                    state.data["endereco"] = "Não disponível"
                    state.data["proprietario"] = "Não disponível"

                state.agent_response = None

                return state

            except Exception as e:
                response = AgentResponse(
                    description=IPTUMessageTemplates.solicitar_inscricao(),
                    payload_schema=InscricaoImobiliariaPayload.model_json_schema(),
                    error_message=f"Inscrição imobiliária inválida: {str(e)}",
                )
                state.agent_response = response
                return state

        # Se já tem inscrição e não foi fornecida nova, continua
        if "inscricao_imobiliaria" in state.data:
            return state

        # Solicita inscrição se não tem nenhuma
        response = AgentResponse(
            description=IPTUMessageTemplates.solicitar_inscricao(),
            payload_schema=InscricaoImobiliariaPayload.model_json_schema(),
        )
        state.agent_response = response

        return state

    @handle_errors
    async def _escolher_ano_exercicio(self, state: ServiceState) -> ServiceState:
        """Coleta o ano de exercício para consulta do IPTU."""
        inscricao = state.data.get("inscricao_imobiliaria", "N/A")
        endereco = state.data.get("endereco", "N/A")
        proprietario = state.data.get("proprietario", "N/A")
        if "ano_exercicio" in state.payload:
            try:
                validated_data = EscolhaAnoPayload.model_validate(state.payload)
                state.data["ano_exercicio"] = validated_data.ano_exercicio
                state.agent_response = None
                return state
            except Exception as e:
                state.agent_response = AgentResponse(
                    description=IPTUMessageTemplates.escolher_ano(
                        inscricao=inscricao,
                        endereco=endereco,
                        proprietario=proprietario,
                    ),
                    payload_schema=EscolhaAnoPayload.model_json_schema(),
                    error_message=f"Ano inválido: {str(e)}",
                )
                return state

        # Se já tem ano escolhido, continua
        if "ano_exercicio" in state.data:
            state.agent_response = None
            return state

        # Solicita escolha do ano
        response = AgentResponse(
            description=IPTUMessageTemplates.escolher_ano(
                inscricao=inscricao, endereco=endereco, proprietario=proprietario
            ),
            payload_schema=EscolhaAnoPayload.model_json_schema(),
        )
        state.agent_response = response
        return state

    @handle_errors
    async def _consultar_guias_disponiveis(self, state: ServiceState) -> ServiceState:
        """Consulta as guias disponíveis para pagamento."""
        # Verifica se a consulta de guias já foi realizada para evitar chamadas duplicadas à API
        if (
            state.internal.get(STATE_HAS_CONSULTED_GUIAS, False)
            and "dados_guias" in state.data
        ):
            state.agent_response = None
            return state

        inscricao = state.data.get("inscricao_imobiliaria")

        if not inscricao:
            state.agent_response = AgentResponse(
                description="❌ Erro interno: inscrição imobiliária não encontrada.",
                error_message=ERROR_INSCRICAO_AUSENTE,
            )
            return state

        # Obtém exercício (ano) escolhido pelo usuário
        exercicio = state.data.get("ano_exercicio")
        if not exercicio:
            state.agent_response = AgentResponse(
                description="❌ Erro interno: ano de exercício não foi escolhido.",
                error_message=ERROR_ANO_AUSENTE,
            )
            return state

        # Consulta guias via API
        try:
            dados_guias = await self.api_service.consultar_guias(inscricao, exercicio)
        except APIUnavailableError as e:
            # API indisponível - não limpa dados, permite retry
            state.agent_response = AgentResponse(
                description=IPTUMessageTemplates.erro_api_indisponivel(str(e)),
                payload_schema=EscolhaAnoPayload.model_json_schema(),
                error_message=str(e),
            )
            return state
        except AuthenticationError as e:
            # Erro de autenticação - problema interno
            state.agent_response = AgentResponse(
                description=IPTUMessageTemplates.erro_autenticacao_api(),
                error_message=str(e),
            )
            return state

        if not dados_guias:
            # Verifica se a inscrição tem formato válido
            if len(inscricao) >= 8 and inscricao.isdigit():
                # Rastreia tentativas falhas para esta inscrição
                key_tentativas = f"{STATE_FAILED_ATTEMPTS_PREFIX}{inscricao}"
                tentativas = state.internal.get(key_tentativas, 0) + 1
                state.internal[key_tentativas] = tentativas

                # Se já tentou MAX_TENTATIVAS_ANO anos diferentes e ainda não encontrou, a inscrição provavelmente não existe
                if tentativas >= MAX_TENTATIVAS_ANO:
                    # Remove os rastros das tentativas e reseta para nova inscrição
                    state.internal.pop(key_tentativas, None)
                    state.agent_response = AgentResponse(
                        description=IPTUMessageTemplates.inscricao_nao_encontrada_apos_tentativas(),
                        payload_schema=InscricaoImobiliariaPayload.model_json_schema(),
                    )
                    # Reset completo para permitir nova entrada
                    state_helpers.reset_completo(state)
                    return state

                # Ainda dentro do limite de tentativas - antes de retornar erro,
                # consulta dívida ativa para verificar se o IPTU foi migrado
                try:
                    logger.info(
                        f"Consultando dívida ativa para inscrição {inscricao} (ano {exercicio} sem guias)"
                    )
                    divida_ativa_info = await self.api_service.get_divida_ativa_info(
                        inscricao
                    )

                    # Se encontrou dívida ativa, informa ao usuário
                    if divida_ativa_info and divida_ativa_info.tem_divida_ativa:
                        logger.info(
                            f"Dívida ativa encontrada para inscrição {inscricao}: {len(divida_ativa_info.cdas)} CDAs, {len(divida_ativa_info.efs)} EFs, {len(divida_ativa_info.parcelamentos)} parcelamentos"
                        )
                        # Salva dados da dívida ativa
                        state.data["divida_ativa_data"] = divida_ativa_info.model_dump()

                        # Remove dados do ano para permitir nova consulta
                        state.data.pop("ano_exercicio", None)
                        state.payload.pop("ano_exercicio", None)
                        state.internal.pop(STATE_HAS_CONSULTED_GUIAS, None)

                        state.agent_response = AgentResponse(
                            description=IPTUMessageTemplates.divida_ativa_encontrada(
                                inscricao=inscricao,
                                ano=exercicio,
                                divida_info=divida_ativa_info,
                            ),
                            payload_schema=EscolhaAnoPayload.model_json_schema(),
                        )
                        return state
                except (APIUnavailableError, AuthenticationError) as e:
                    # Se falhar a consulta de dívida ativa por erro de API, apenas loga e continua
                    logger.warning(
                        f"Falha ao consultar dívida ativa (API): {str(e)}. Continuando com fluxo normal."
                    )
                except Exception as e:
                    # Se falhar a consulta de dívida ativa por outro erro, apenas loga e continua
                    logger.warning(
                        f"Falha ao consultar dívida ativa: {str(e)}. Continuando com fluxo normal."
                    )

                # Se não encontrou dívida ativa ou houve erro, retorna mensagem padrão
                state.data.pop("ano_exercicio", None)
                state.payload.pop(
                    "ano_exercicio", None
                )  # Remove do payload também para evitar loop
                state.internal.pop(STATE_HAS_CONSULTED_GUIAS, None)

                state.agent_response = AgentResponse(
                    description=IPTUMessageTemplates.nenhuma_guia_encontrada(
                        inscricao, exercicio
                    ),
                    payload_schema=EscolhaAnoPayload.model_json_schema(),
                )
                return state
            else:
                # Inscrição com formato inválido - remove e pede nova
                state.agent_response = AgentResponse(
                    description=IPTUMessageTemplates.inscricao_nao_encontrada(),
                    payload_schema=InscricaoImobiliariaPayload.model_json_schema(),
                )
                # Reset completo para permitir nova entrada
                state_helpers.reset_completo(state)
                return state

        # Se chegou aqui, encontrou guias - limpa contador de tentativas falhas
        inscricao_clean = inscricao.replace(" ", "").replace("-", "").replace(".", "")
        key_tentativas = f"{STATE_FAILED_ATTEMPTS_PREFIX}{inscricao_clean}"
        state.internal.pop(key_tentativas, None)

        # Salva dados das guias real
        state.data["dados_guias"] = dados_guias.model_dump()
        state.internal[STATE_HAS_CONSULTED_GUIAS] = True
        # Não para o fluxo aqui - continua para mostrar as guias
        state.agent_response = None

        return state

    @handle_errors
    async def _usuario_escolhe_guias_iptu(self, state: ServiceState) -> ServiceState:
        """Usuário escolhe qual guia de IPTU quer pagar (por número da guia)."""
        if "guia_escolhida" in state.payload:
            try:
                validated_data = EscolhaGuiasIPTUPayload.model_validate(state.payload)
                state.data["guia_escolhida"] = validated_data.guia_escolhida
                state.agent_response = None
                return state
            except Exception as e:
                try:
                    guias_info = self._buscar_guias_detalhadas(state)
                    state.agent_response = AgentResponse(
                        description=guias_info,
                        payload_schema=EscolhaGuiasIPTUPayload.model_json_schema(),
                        error_message=f"Seleção inválida: {str(e)}",
                    )
                except ValueError as ve:
                    # Se erro ao buscar guias, retorna erro apropriado
                    state.agent_response = AgentResponse(
                        description=IPTUMessageTemplates.erro_dados_guias_invalidos(),
                        payload_schema=InscricaoImobiliariaPayload.model_json_schema(),
                        error_message=str(ve),
                    )
                    state_helpers.reset_completo(state)
                return state

        # Se já tem guia escolhida, continua
        if "guia_escolhida" in state.data:
            return state

        # Busca e apresenta informações detalhadas das guias
        try:
            guias_info = self._buscar_guias_detalhadas(state)
            response = AgentResponse(
                description=guias_info,
                payload_schema=EscolhaGuiasIPTUPayload.model_json_schema(),
            )
            state.agent_response = response
        except ValueError as e:
            # Se erro ao buscar guias, retorna erro apropriado
            state.agent_response = AgentResponse(
                description=IPTUMessageTemplates.erro_dados_guias_invalidos(),
                payload_schema=InscricaoImobiliariaPayload.model_json_schema(),
                error_message=str(e),
            )
            state_helpers.reset_completo(state)

        return state

    def _buscar_guias_detalhadas(self, state: ServiceState) -> str:
        """Formata informações detalhadas das guias disponíveis usando dados já consultados."""
        dados_guias = state.data.get("dados_guias", {})
        endereco = state.data.get("endereco", "N/A")
        proprietario = state.data.get("proprietario", "N/A")

        # Validação: Se não temos dados de guias válidos, retorna erro
        if not dados_guias or "guias" not in dados_guias:
            raise ValueError("Dados das guias não encontrados ou inválidos")

        guias_disponiveis = dados_guias.get("guias", [])

        # Validação: Se não há guias disponíveis, retorna erro
        if not guias_disponiveis or len(guias_disponiveis) == 0:
            raise ValueError("Nenhuma guia disponível encontrada")

        # Prepara dados para o template
        guias_formatadas = utils.preparar_dados_guias_para_template(
            dados_guias, self.api_service
        )

        return IPTUMessageTemplates.dados_imovel(
            inscricao=dados_guias.get("inscricao_imobiliaria", ""),
            proprietario=proprietario,
            endereco=endereco,
            exercicio=dados_guias.get("exercicio", ""),
            guias=guias_formatadas,
        )

    @handle_errors
    async def _consultar_cotas(self, state: ServiceState) -> ServiceState:
        """Consulta as cotas disponíveis para a guia selecionada via API."""
        # Se já temos dados de cotas, pula a consulta
        if "dados_cotas" in state.data:
            return state

        # Valida dados necessários para consulta
        inscricao = state.data.get("inscricao_imobiliaria")
        exercicio = state.data.get("ano_exercicio")
        guia_escolhida = state.data.get("guia_escolhida")

        if not all([inscricao, exercicio, guia_escolhida]):
            state.agent_response = AgentResponse(
                description="❌ Erro interno: dados para consulta de cotas ausentes.",
                error_message="Inscrição, exercício ou guia não encontrados.",
            )
            return state

        # Faz consulta via API
        try:
            dados_cotas = await self.api_service.obter_cotas(
                str(inscricao), int(exercicio or 2025), str(guia_escolhida)
            )
        except APIUnavailableError as e:
            # API indisponível - volta para seleção de guias
            state.data.pop("guia_escolhida", None)
            state.agent_response = AgentResponse(
                description=IPTUMessageTemplates.erro_api_indisponivel(str(e)),
                payload_schema=EscolhaGuiasIPTUPayload.model_json_schema(),
                error_message=str(e),
            )
            return state
        except AuthenticationError as e:
            # Erro de autenticação - problema interno
            state.agent_response = AgentResponse(
                description=IPTUMessageTemplates.erro_autenticacao_api(),
                error_message=str(e),
            )
            return state

        if not dados_cotas or not dados_cotas.cotas:
            # Nenhuma cota encontrada para a guia selecionada
            # Remove dados da guia escolhida e reseta campos relacionados
            state.data.pop("guia_escolhida", None)
            state.data.pop("dados_cotas", None)

            state.agent_response = AgentResponse(
                description=IPTUMessageTemplates.nenhuma_cota_encontrada(
                    str(guia_escolhida)
                ),
                payload_schema=EscolhaGuiasIPTUPayload.model_json_schema(),
            )
            return state

        # Salva dados das cotas
        state.data["dados_cotas"] = dados_cotas.model_dump()
        cotas_em_aberto = [c for c in dados_cotas.cotas if not c.esta_paga]

        if not cotas_em_aberto:
            # Todas as cotas desta guia já foram quitadas
            # Remove dados da guia escolhida e reseta campos relacionados
            state.data.pop("guia_escolhida", None)
            state.data.pop("dados_cotas", None)

            state.agent_response = AgentResponse(
                description=IPTUMessageTemplates.cotas_quitadas(str(guia_escolhida)),
                payload_schema=EscolhaGuiasIPTUPayload.model_json_schema(),
            )
            return state

        # # Se há apenas uma cota, seleciona automaticamente
        # if len(cotas_em_aberto) == 1:
        #     state.data["cotas_escolhidas"] = [cotas_em_aberto[0].numero_cota]
        #     state.internal[STATE_IS_SINGLE_QUOTA_FLOW] = True
        #     state.agent_response = None
        #     return state

        # Consulta realizada com sucesso, próximo nó irá apresentar escolhas
        state.agent_response = None
        return state

    @handle_errors
    async def _usuario_escolhe_cotas_iptu(self, state: ServiceState) -> ServiceState:
        """Permite ao usuário escolher as cotas a pagar."""
        # Se já temos cotas escolhidas, não precisa escolher novamente
        if "cotas_escolhidas" in state.data:
            return state

        # Processa payload se presente
        if "cotas_escolhidas" in state.payload:
            validated_data = EscolhaCotasParceladasPayload.model_validate(state.payload)
            state.data["cotas_escolhidas"] = validated_data.cotas_escolhidas
            state.agent_response = None
            return state

        # Carrega dados das cotas do state
        dados_cotas_dict = state.data.get("dados_cotas")
        if not dados_cotas_dict:
            state.agent_response = AgentResponse(
                description="❌ Erro interno: dados de cotas não encontrados.",
                error_message="Dados de cotas não carregados.",
            )
            return state

        # Reconstrói objeto DadosCotas
        dados_cotas = DadosCotas(**dados_cotas_dict)

        # Prepara dados das cotas para o template
        cotas_formatadas = utils.preparar_dados_cotas_para_template(dados_cotas)
        valor_total = sum(c["valor_numerico"] for c in cotas_formatadas)

        # Apresenta opções de cotas para escolha
        cotas_texto = IPTUMessageTemplates.selecionar_cotas(
            cotas_formatadas, valor_total
        )

        state.agent_response = AgentResponse(
            description=cotas_texto,
            payload_schema=EscolhaCotasParceladasPayload.model_json_schema(),
        )
        return state

    @handle_errors
    async def _perguntar_formato_darm(self, state: ServiceState) -> ServiceState:
        """Pergunta se o usuário quer um boleto único ou separado para as cotas selecionadas."""
        if STATE_USE_SEPARATE_DARM in state.internal:
            return state

        cotas_escolhidas = state.data.get("cotas_escolhidas", [])
        if len(cotas_escolhidas) <= 1:
            state.internal[STATE_USE_SEPARATE_DARM] = False  # Padrão para cota única
            return state

        if "darm_separado" in state.payload:
            try:
                validated_data = EscolhaFormatoDarmPayload.model_validate(state.payload)
                state.internal[STATE_USE_SEPARATE_DARM] = validated_data.darm_separado
                state.agent_response = None
                return state
            except Exception as e:
                state.agent_response = AgentResponse(
                    description=IPTUMessageTemplates.escolher_formato_darm(),
                    payload_schema=EscolhaFormatoDarmPayload.model_json_schema(),
                    error_message=f"Formato inválido: {str(e)}",
                )
                return state

        state.agent_response = AgentResponse(
            description=IPTUMessageTemplates.escolher_formato_darm(),
            payload_schema=EscolhaFormatoDarmPayload.model_json_schema(),
        )
        return state

    @handle_errors
    async def _confirmacao_dados_pagamento(self, state: ServiceState) -> ServiceState:
        """Confirma os dados coletados para gerar o pagamento."""
        if state.internal.get(STATE_IS_DATA_CONFIRMED, False):
            return state

        # Validação de campos obrigatórios
        campo_faltante = state_helpers.validar_dados_obrigatorios(
            state, ["inscricao_imobiliaria", "guia_escolhida", "cotas_escolhidas"]
        )
        if campo_faltante:
            state.agent_response = AgentResponse(
                description=IPTUMessageTemplates.erro_interno(
                    f"Campo obrigatório faltante: {campo_faltante}"
                ),
                payload_schema=InscricaoImobiliariaPayload.model_json_schema(),
            )
            state_helpers.reset_completo(state)
            return state

        inscricao = state.data["inscricao_imobiliaria"]
        guia_escolhida = state.data.get("guia_escolhida", "N/A")
        cotas_escolhidas = state.data.get("cotas_escolhidas", [])
        darm_separado = state.internal.get(STATE_USE_SEPARATE_DARM, False)
        endereco = state.data.get("endereco", "N/A")
        proprietario = state.data.get("proprietario", "N/A")

        num_boletos = utils.calcular_numero_boletos(
            darm_separado, len(cotas_escolhidas)
        )

        resumo_texto = IPTUMessageTemplates.confirmacao_dados(
            inscricao=inscricao,
            endereco=endereco,
            proprietario=proprietario,
            guia_escolhida=guia_escolhida,
            cotas_escolhidas=cotas_escolhidas,
            num_boletos=num_boletos,
        )

        if "confirmacao" not in state.payload:
            state.agent_response = AgentResponse(
                description=resumo_texto,
                payload_schema=ConfirmacaoDadosPayload.model_json_schema(),
            )
            return state

        validated_data = ConfirmacaoDadosPayload.model_validate(state.payload)
        if not validated_data.confirmacao:
            state.agent_response = AgentResponse(
                description=IPTUMessageTemplates.dados_nao_confirmados(),
                payload_schema=InscricaoImobiliariaPayload.model_json_schema(),
            )
            # Reset completo para recomeçar, mantendo a inscrição
            state_helpers.reset_completo(state, manter_inscricao=True)
            return state

        state.internal[STATE_IS_DATA_CONFIRMED] = True
        state.agent_response = None
        return state

    @handle_errors
    async def _gerar_darm(self, state: ServiceState) -> ServiceState:
        """Gera DARM(s) após confirmação dos dados."""
        if "guias_geradas" in state.data:
            return state

        # Validação de campos obrigatórios
        campo_faltante = state_helpers.validar_dados_obrigatorios(
            state,
            [
                "inscricao_imobiliaria",
                "guia_escolhida",
                "cotas_escolhidas",
                "ano_exercicio",
            ],
        )
        if campo_faltante:
            state.agent_response = AgentResponse(
                description=IPTUMessageTemplates.erro_interno(
                    f"Campo obrigatório faltante: {campo_faltante}"
                ),
                payload_schema=InscricaoImobiliariaPayload.model_json_schema(),
            )
            state_helpers.reset_completo(state)
            return state

        inscricao = state.data["inscricao_imobiliaria"]
        guia_escolhida = state.data["guia_escolhida"]
        cotas_escolhidas = state.data["cotas_escolhidas"]
        exercicio = state.data["ano_exercicio"]
        darm_separado = state.internal.get(STATE_USE_SEPARATE_DARM, False)

        guias_geradas = []

        cotas_a_processar = []
        if darm_separado:
            cotas_a_processar = [[c] for c in cotas_escolhidas]
        else:
            cotas_a_processar = [cotas_escolhidas]

        for cotas_para_darm in cotas_a_processar:
            try:
                dados_darm = await self.api_service.consultar_darm(
                    inscricao_imobiliaria=inscricao,
                    exercicio=exercicio,
                    numero_guia=guia_escolhida,
                    cotas_selecionadas=cotas_para_darm,
                )

                if not dados_darm or not dados_darm.darm:
                    # Falha na geração do DARM - reseta dados de cotas e volta para seleção de cotas
                    state_helpers.reset_para_selecao_cotas(state)

                    state.agent_response = AgentResponse(
                        description=IPTUMessageTemplates.erro_gerar_darm(
                            cotas_para_darm
                        ),
                        payload_schema=EscolhaCotasParceladasPayload.model_json_schema(),
                    )
                    return state

                # Tenta baixar o PDF, mas continua mesmo se falhar
                try:
                    urls = await self.api_service.download_pdf_darm(
                        inscricao_imobiliaria=inscricao,
                        exercicio=exercicio,
                        numero_guia=guia_escolhida,
                        cotas_selecionadas=cotas_para_darm,
                    )
                except (APIUnavailableError, AuthenticationError) as e:
                    # Se falhar download do PDF, continua sem o PDF
                    logger.warning(f"Falha ao baixar PDF do DARM: {str(e)}")
                    urls = "Não disponível (erro ao baixar)"

                guias_geradas.append(
                    {
                        "tipo": "darm",
                        "numero_guia": guia_escolhida,
                        "cotas": ", ".join(cotas_para_darm),
                        "valor": dados_darm.darm.valor_numerico,
                        "vencimento": dados_darm.darm.data_vencimento,
                        "codigo_barras": dados_darm.darm.codigo_barras,
                        "linha_digitavel": dados_darm.darm.sequencia_numerica,
                        "pdf": urls,
                    }
                )

            except APIUnavailableError as e:
                # API indisponível - reseta dados de cotas e volta para seleção de cotas
                state_helpers.reset_para_selecao_cotas(state)

                state.agent_response = AgentResponse(
                    description=IPTUMessageTemplates.erro_api_indisponivel(str(e)),
                    payload_schema=EscolhaCotasParceladasPayload.model_json_schema(),
                    error_message=str(e),
                )
                return state
            except AuthenticationError as e:
                # Erro de autenticação - problema interno
                state.agent_response = AgentResponse(
                    description=IPTUMessageTemplates.erro_autenticacao_api(),
                    error_message=str(e),
                )
                return state
            except Exception as e:
                # Outro erro - reseta dados de cotas e volta para seleção de cotas
                state_helpers.reset_para_selecao_cotas(state)

                state.agent_response = AgentResponse(
                    description=IPTUMessageTemplates.erro_processar_pagamento(
                        cotas_para_darm, str(e)
                    ),
                    payload_schema=EscolhaCotasParceladasPayload.model_json_schema(),
                )
                return state

        if not guias_geradas:
            # Nenhuma guia foi gerada com sucesso - reseta dados de cotas
            state_helpers.reset_para_selecao_cotas(state)

            state.agent_response = AgentResponse(
                description=IPTUMessageTemplates.nenhum_boleto_gerado(),
                payload_schema=EscolhaCotasParceladasPayload.model_json_schema(),
            )
            return state

        state.data["guias_geradas"] = guias_geradas

        # Não define agent_response aqui, pois o workflow deve continuar para pergunta_mesma_guia
        # A resposta será definida no método _pergunta_mesma_guia
        return state

    def _tem_mais_cotas_disponiveis(self, state: ServiceState) -> bool:
        """Verifica se há mais cotas disponíveis da guia atual para pagar."""
        return utils.tem_mais_cotas_disponiveis(state)

    def _tem_outras_guias_disponiveis(self, state: ServiceState) -> bool:
        """Verifica se há outras guias disponíveis no imóvel."""
        return utils.tem_outras_guias_disponiveis(state)

    @handle_errors
    async def _pergunta_mesma_guia(self, state: ServiceState) -> ServiceState:
        """
        Roteador condicional inteligente pós-geração de boletos:
        1. Se há mais cotas disponíveis da mesma guia → redireciona para pergunta_mais_cotas
        2. Se há outras guias disponíveis no imóvel → redireciona para pergunta_outras_guias
        3. Caso contrário → redireciona para pergunta_outro_imovel
        """
        # Analisa contexto para determinar próxima pergunta
        tem_mais_cotas = self._tem_mais_cotas_disponiveis(state)
        tem_outras_guias = self._tem_outras_guias_disponiveis(state)

        # Define o tipo de pergunta baseado no contexto
        if tem_mais_cotas:
            state.internal[STATE_NEXT_QUESTION_TYPE] = QUESTION_TYPE_MORE_QUOTAS
        elif tem_outras_guias:
            state.internal[STATE_NEXT_QUESTION_TYPE] = QUESTION_TYPE_OTHER_GUIAS
        else:
            state.internal[STATE_NEXT_QUESTION_TYPE] = QUESTION_TYPE_OTHER_PROPERTY

        # Clear agent_response to allow proper routing
        state.agent_response = None
        return state

    @handle_errors
    async def _pergunta_mais_cotas(self, state: ServiceState) -> ServiceState:
        """Pergunta se quer pagar mais cotas da mesma guia."""
        # Se já foi respondido, não pergunta novamente
        if STATE_WANTS_MORE_QUOTAS in state.internal:
            return state

        # Se tem payload com resposta, processa
        if "mais_cotas" in state.payload:
            validated_data = EscolhaMaisCotasPayload.model_validate(state.payload)
            state.internal[STATE_WANTS_MORE_QUOTAS] = validated_data.mais_cotas
            state.agent_response = None
            return state

        # Monta descrição com os boletos gerados + pergunta específica
        description_text = self._gerar_descricao_boletos_gerados(state)
        description_text = IPTUMessageTemplates.perguntar_mais_cotas(description_text)

        response = AgentResponse(
            description=description_text,
            payload_schema=EscolhaMaisCotasPayload.model_json_schema(),
        )
        state.agent_response = response
        return state

    @handle_errors
    async def _pergunta_outras_guias(self, state: ServiceState) -> ServiceState:
        """Pergunta se quer pagar outras guias do mesmo imóvel."""
        # Se já foi respondido, não pergunta novamente
        if STATE_WANTS_OTHER_GUIAS in state.internal:
            return state

        # Se tem payload com resposta, processa
        if "outras_guias" in state.payload:
            validated_data = EscolhaOutrasGuiasPayload.model_validate(state.payload)
            state.internal[STATE_WANTS_OTHER_GUIAS] = validated_data.outras_guias
            state.agent_response = None
            return state

        # Monta descrição com os boletos gerados + pergunta específica
        description_text = self._gerar_descricao_boletos_gerados(state)
        description_text = IPTUMessageTemplates.perguntar_outras_guias(description_text)

        response = AgentResponse(
            description=description_text,
            payload_schema=EscolhaOutrasGuiasPayload.model_json_schema(),
        )
        state.agent_response = response
        return state

    def _gerar_descricao_boletos_gerados(self, state: ServiceState) -> str:
        """Gera a descrição padrão dos boletos gerados."""
        guias_geradas = state.data.get("guias_geradas", [])
        inscricao = state.data.get("inscricao_imobiliaria", "N/A")

        # Prepara dados dos boletos para o template
        boletos_formatados = utils.preparar_dados_boletos_para_template(guias_geradas)

        return IPTUMessageTemplates.boletos_gerados(boletos_formatados, inscricao)

    @handle_errors
    async def _pergunta_outro_imovel(self, state: ServiceState) -> ServiceState:
        """Pergunta se quer gerar guia para outro imóvel."""
        # Se já foi respondido, não pergunta novamente
        if STATE_WANTS_OTHER_PROPERTY in state.internal:
            return state

        # Se tem payload com resposta, processa
        if "outro_imovel" in state.payload:
            validated_data = EscolhaOutroImovelPayload.model_validate(state.payload)
            state.internal[STATE_WANTS_OTHER_PROPERTY] = validated_data.outro_imovel
            state.agent_response = None
            return state

        response = AgentResponse(
            description=IPTUMessageTemplates.perguntar_outro_imovel(),
            payload_schema=EscolhaOutroImovelPayload.model_json_schema(),
        )
        state.agent_response = response

        return state

    @handle_errors
    async def _reset_para_mais_cotas(self, state: ServiceState) -> ServiceState:
        """Reset seletivo para pagar mais cotas da mesma guia."""
        # Reset apenas dos campos relacionados à seleção de cotas e posteriores
        fields_to_reset = {
            "data": ["cotas_escolhidas", "dados_darm", "guias_geradas"],
            "internal": [
                STATE_WANTS_MORE_QUOTAS,
                STATE_WANTS_OTHER_GUIAS,
                STATE_WANTS_OTHER_PROPERTY,
                STATE_NEXT_QUESTION_TYPE,
            ],
        }

        state_helpers.reset_completo(
            state, manter_inscricao=True, fields=fields_to_reset
        )
        return state

    @handle_errors
    async def _reset_para_outras_guias(self, state: ServiceState) -> ServiceState:
        """Reset seletivo para pagar outras guias do mesmo imóvel."""
        # Reset dos campos relacionados à seleção de guia e posteriores
        fields_to_reset = {
            "data": [
                "guia_escolhida",
                "dados_cotas",
                "cotas_escolhidas",
                "dados_darm",
                "guias_geradas",
            ],
            "internal": [
                STATE_WANTS_MORE_QUOTAS,
                STATE_WANTS_OTHER_GUIAS,
                STATE_WANTS_OTHER_PROPERTY,
                STATE_NEXT_QUESTION_TYPE,
            ],
        }

        state_helpers.reset_completo(
            state, manter_inscricao=True, fields=fields_to_reset
        )
        return state

    @handle_errors
    async def _reset_para_mesma_guia(self, state: ServiceState) -> ServiceState:
        """Reset dados para gerar nova guia do mesmo imóvel."""
        # Reset completo mantendo inscrição e dados das guias consultadas
        inscricao = state.data.get("inscricao_imobiliaria")
        dados_guias = state.data.get("dados_guias")

        state_helpers.reset_completo(state)

        # Restaura apenas inscrição e dados das guias
        if inscricao:
            state.data["inscricao_imobiliaria"] = inscricao
        if dados_guias:
            state.data["dados_guias"] = dados_guias
            state.internal[STATE_HAS_CONSULTED_GUIAS] = True

        return state

    @handle_errors
    async def _reset_para_outro_imovel(self, state: ServiceState) -> ServiceState:
        """Reset completo para outro imóvel."""
        # Reset completo de tudo
        state_helpers.reset_completo(state)
        return state

    @handle_errors
    async def _finalizar_interacao(self, state: ServiceState) -> ServiceState:
        """Finaliza a interação e faz reset automático dos dados."""
        # Mensagem de finalização
        response = AgentResponse(
            description=IPTUMessageTemplates.finalizacao(),
        )
        state.agent_response = response

        # Reset automático completo no final da interação
        state_helpers.reset_completo(state)

        return state

    # --- Roteadores Condicionais ---

    def _decide_after_data_collection(self, state: ServiceState):
        """Roteador genérico para nós de coleta de dados."""
        if state.agent_response is not None:
            return END
        return "continue"

    def _route_after_inscricao(self, state: ServiceState) -> str:
        """Roteamento após coleta da inscrição."""
        # Se tem inscrição, vai para seleção de ano
        if "inscricao_imobiliaria" in state.data:
            return "escolher_ano"
        # Se não tem inscrição, fica aguardando input
        return "informar_inscricao"

    def _route_consulta_guias(self, state: ServiceState) -> str:
        """
        Roteamento após consulta de guias.

        Lógica de roteamento:
        1. Se guias foram encontradas → vai para seleção de guias
        2. Se consulta falhou MAS já tem mensagem de erro definida → END (não sobrescreve)
        3. Se consulta falhou e não tem ano → volta para seleção de ano
        4. Se consulta falhou → volta para informar inscrição

        A verificação de agent_response é crítica para evitar sobrescrever mensagens
        de erro específicas (ex: "nenhuma guia encontrada para o ano X").
        """
        # Se não tem dados de guias, significa que a consulta falhou
        if "dados_guias" not in state.data:
            # Se já tem uma mensagem de erro definida, para o fluxo (END)
            # para não sobrescrever a mensagem de erro específica
            if state.agent_response is not None:
                return END

            # Se tem inscrição válida mas não tem ano, volta para escolha do ano
            if (
                "inscricao_imobiliaria" in state.data
                and "ano_exercicio" not in state.data
            ):
                return "escolher_ano"
            # Caso contrário, volta para informar inscrição
            return "informar_inscricao"
        return "usuario_escolhe_guias"

    def _route_consulta_cotas(self, state: ServiceState) -> str:
        """Roteamento após consulta de cotas."""
        # Se agent_response foi definido, significa que ocorreu erro/reset e precisa voltar
        if state.agent_response is not None:
            return END  # Para e espera nova seleção de guia
        # Se não tem dados de cotas válidos, volta para seleção de guias
        if "dados_cotas" not in state.data:
            return "usuario_escolhe_guias"
        return "usuario_escolhe_cotas"

    def _route_after_mesma_guia(self, state: ServiceState) -> str:
        """Roteamento após pergunta sobre mesma guia - agora é um roteador condicional."""
        # Se já determinou o tipo de pergunta, roteia adequadamente
        tipo_pergunta = state.internal.get(STATE_NEXT_QUESTION_TYPE)

        if tipo_pergunta == QUESTION_TYPE_MORE_QUOTAS:
            return "pergunta_mais_cotas"
        elif tipo_pergunta == QUESTION_TYPE_OTHER_GUIAS:
            return "pergunta_outras_guias"
        elif tipo_pergunta == QUESTION_TYPE_OTHER_PROPERTY:
            return "pergunta_outro_imovel"

        # Se não determinou ainda, fica no END (não deveria acontecer)
        return END

    def _route_after_outro_imovel(self, state: ServiceState) -> str:
        """Roteamento após pergunta sobre outro imóvel."""
        # Se ainda não respondeu, fica no nó atual para aguardar resposta
        if STATE_WANTS_OTHER_PROPERTY not in state.internal:
            return END

        if state.internal.get(STATE_WANTS_OTHER_PROPERTY, False):
            return "reset_outro_imovel"
        return "finalizar_interacao"  # Finaliza com reset automático

    def _route_after_mais_cotas(self, state: ServiceState) -> str:
        """Roteamento após pergunta sobre mais cotas."""
        # Se ainda não respondeu, fica no nó atual para aguardar resposta
        if STATE_WANTS_MORE_QUOTAS not in state.internal:
            return END

        if state.internal.get(STATE_WANTS_MORE_QUOTAS, False):
            return "reset_mais_cotas"

        # Se não quer mais cotas, verifica se tem outras guias
        if self._tem_outras_guias_disponiveis(state):
            return "pergunta_outras_guias"

        # Se não tem outras guias, vai direto para pergunta sobre outro imóvel
        return "pergunta_outro_imovel"

    def _route_after_outras_guias(self, state: ServiceState) -> str:
        """Roteamento após pergunta sobre outras guias."""
        # Se ainda não respondeu, fica no nó atual para aguardar resposta
        if STATE_WANTS_OTHER_GUIAS not in state.internal:
            return END

        if state.internal.get(STATE_WANTS_OTHER_GUIAS, False):
            return "reset_outras_guias"

        # Se não quer outras guias, vai para pergunta sobre outro imóvel
        return "pergunta_outro_imovel"

    # --- Construção do Grafo ---

    def build_graph(self) -> StateGraph[ServiceState]:
        """Constrói o grafo do workflow IPTU."""
        graph = StateGraph(ServiceState)

        # Adiciona todos os nós
        graph.add_node("informar_inscricao", self._informar_inscricao_imobiliaria)
        graph.add_node("escolher_ano", self._escolher_ano_exercicio)
        graph.add_node("consultar_guias", self._consultar_guias_disponiveis)
        graph.add_node("usuario_escolhe_guias", self._usuario_escolhe_guias_iptu)
        graph.add_node("consultar_cotas", self._consultar_cotas)
        graph.add_node("usuario_escolhe_cotas", self._usuario_escolhe_cotas_iptu)
        graph.add_node("perguntar_formato_darm", self._perguntar_formato_darm)
        graph.add_node("confirmacao_dados", self._confirmacao_dados_pagamento)
        graph.add_node("gerar_darm", self._gerar_darm)
        graph.add_node("pergunta_mesma_guia", self._pergunta_mesma_guia)
        graph.add_node("pergunta_mais_cotas", self._pergunta_mais_cotas)
        graph.add_node("pergunta_outras_guias", self._pergunta_outras_guias)
        graph.add_node("pergunta_outro_imovel", self._pergunta_outro_imovel)
        graph.add_node("reset_mesma_guia", self._reset_para_mesma_guia)
        graph.add_node("reset_mais_cotas", self._reset_para_mais_cotas)
        graph.add_node("reset_outras_guias", self._reset_para_outras_guias)
        graph.add_node("reset_outro_imovel", self._reset_para_outro_imovel)
        graph.add_node("finalizar_interacao", self._finalizar_interacao)

        # Define ponto de entrada
        graph.set_entry_point("informar_inscricao")

        # Fluxo principal
        graph.add_conditional_edges(
            "informar_inscricao",
            self._decide_after_data_collection,
            {"continue": "escolher_ano", END: END},
        )
        graph.add_conditional_edges(
            "escolher_ano",
            self._decide_after_data_collection,
            {"continue": "consultar_guias", END: END},
        )
        graph.add_conditional_edges(
            "consultar_guias",
            self._route_consulta_guias,
            {
                "usuario_escolhe_guias": "usuario_escolhe_guias",
                "escolher_ano": "escolher_ano",
                "informar_inscricao": "informar_inscricao",
                END: END,
            },
        )
        graph.add_conditional_edges(
            "usuario_escolhe_guias",
            self._decide_after_data_collection,
            {"continue": "consultar_cotas", END: END},
        )
        graph.add_conditional_edges(
            "consultar_cotas",
            self._route_consulta_cotas,
            {
                "usuario_escolhe_cotas": "usuario_escolhe_cotas",
                "usuario_escolhe_guias": "usuario_escolhe_guias",
                END: END,
            },
        )
        graph.add_conditional_edges(
            "usuario_escolhe_cotas",
            self._decide_after_data_collection,
            {"continue": "perguntar_formato_darm", END: END},
        )
        graph.add_conditional_edges(
            "perguntar_formato_darm",
            self._decide_after_data_collection,
            {"continue": "confirmacao_dados", END: END},
        )
        graph.add_conditional_edges(
            "confirmacao_dados",
            self._decide_after_data_collection,
            {"continue": "gerar_darm", END: END},
        )
        graph.add_conditional_edges(
            "gerar_darm",
            self._route_gerar_darm,
            {
                "pergunta_mesma_guia": "pergunta_mesma_guia",
                "usuario_escolhe_cotas": "usuario_escolhe_cotas",
                END: END,
            },
        )

        graph.add_conditional_edges(
            "pergunta_mesma_guia",
            self._route_after_mesma_guia,
            {
                "pergunta_mais_cotas": "pergunta_mais_cotas",
                "pergunta_outras_guias": "pergunta_outras_guias",
                "pergunta_outro_imovel": "pergunta_outro_imovel",
                END: END,
            },
        )

        graph.add_conditional_edges(
            "pergunta_mais_cotas",
            self._route_after_mais_cotas,
            {
                "reset_mais_cotas": "reset_mais_cotas",
                "pergunta_outras_guias": "pergunta_outras_guias",
                "pergunta_outro_imovel": "pergunta_outro_imovel",
                END: END,
            },
        )
        graph.add_edge("reset_mais_cotas", "usuario_escolhe_cotas")

        graph.add_conditional_edges(
            "pergunta_outras_guias",
            self._route_after_outras_guias,
            {
                "reset_outras_guias": "reset_outras_guias",
                "pergunta_outro_imovel": "pergunta_outro_imovel",
                END: END,
            },
        )
        graph.add_edge("reset_outras_guias", "usuario_escolhe_guias")

        graph.add_conditional_edges(
            "pergunta_outro_imovel",
            self._route_after_outro_imovel,
            {
                "reset_outro_imovel": "reset_outro_imovel",
                "finalizar_interacao": "finalizar_interacao",
                END: END,
            },
        )
        graph.add_edge("reset_outro_imovel", "informar_inscricao")
        graph.add_edge("finalizar_interacao", END)

        return graph

    def _route_gerar_darm(self, state: ServiceState) -> str:
        """Roteamento após geração de DARM."""
        # Se agent_response foi definido, significa que ocorreu erro/reset e precisa voltar
        if state.agent_response is not None:
            return END  # Para e espera nova seleção de cotas
        # Se tem guias geradas, continua para pergunta_mesma_guia
        if "guias_geradas" in state.data:
            return "pergunta_mesma_guia"
        # Se chegou aqui sem agent_response e sem guias_geradas, há um problema
        return END
