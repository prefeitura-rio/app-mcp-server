"""
Workflow IPTU Ano Vigente - Prefeitura do Rio de Janeiro

Implementa o fluxo completo de consulta e emissão de guias de IPTU
seguindo o fluxograma oficial da Prefeitura do Rio.
"""

import os
from langgraph.graph import StateGraph, END
from loguru import logger

from src.tools.multi_step_service.core import (
    AgentResponse,
    BaseWorkflow,
    ServiceState,
    handle_errors,
)

from src.tools.multi_step_service.workflows.iptu_pagamento.core.models import (
    InscricaoImobiliariaPayload,
    EscolhaAnoPayload,
    EscolhaGuiasIPTUPayload,
    EscolhaCotasParceladasPayload,
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
    InvalidInscricaoError,
)
from src.tools.multi_step_service.workflows.iptu_pagamento.templates import (
    IPTUMessageTemplates,
)
from src.tools.multi_step_service.workflows.iptu_pagamento.helpers import utils
from src.tools.multi_step_service.workflows.iptu_pagamento.helpers import state_helpers
from src.tools.multi_step_service.workflows.iptu_pagamento.core.constants import (
    FAKE_API_ENV_VAR,
    MAX_TENTATIVAS_ANO,
    STATE_IS_DATA_CONFIRMED,
    STATE_HAS_CONSULTED_GUIAS,
    STATE_USE_SEPARATE_DARM,
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

    # Navegação não-linear: permite usuário "voltar" para steps anteriores
    automatic_resets = True

    # Define ordem dos steps principais do workflow
    step_order = [
        "inscricao_imobiliaria",
        "ano_exercicio",
        "guia_escolhida",
        "cotas_escolhidas",
    ]

    # Define o que cada campo invalida quando muda
    # Ex: Se ano_exercicio muda, remove dados_guias, guia_escolhida, etc.
    step_dependencies = {
        "inscricao_imobiliaria": [
            "endereco",
            "proprietario",
            "ano_exercicio",
            "dados_guias",
            "guia_escolhida",
            "dados_cotas",
            "cotas_escolhidas",
            "divida_ativa_data",
        ],
        "ano_exercicio": [
            "dados_guias",
            "guia_escolhida",
            "dados_cotas",
            "cotas_escolhidas",
            "divida_ativa_data",
        ],
        "guia_escolhida": ["dados_cotas", "cotas_escolhidas"],
        "cotas_escolhidas": [],  # Último step, não invalida nada
    }

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
        self._use_fake_api = use_fake_api or force_fake_api

        # API service será criado no primeiro acesso via propriedade
        self._api_service = None

    @property
    def api_service(self):
        """
        Propriedade lazy para criar API service com user_id correto.
        O user_id é injetado no execute(), então criamos o service apenas quando necessário.
        """
        if self._api_service is None:
            if not self._use_fake_api:
                # Cria API service com user_id do workflow
                self._api_service = IPTUAPIService(user_id=self._user_id)
            else:
                self._api_service = IPTUAPIServiceFake()
        return self._api_service

    # --- Nós do Grafo ---

    @handle_errors
    async def _informar_inscricao_imobiliaria(
        self, state: ServiceState
    ) -> ServiceState:
        """Coleta a inscrição imobiliária do usuário."""
        if "inscricao_imobiliaria" in state.payload:
            validated_data = InscricaoImobiliariaPayload.model_validate(state.payload)
            inscricao_clean = validated_data.inscricao_imobiliaria
            # ANTES de salvar a inscrição, valida consultando dados do imóvel
            logger.debug(
                f"🔍 Validando inscrição e buscando dados do imóvel: {inscricao_clean}"
            )
            try:
                dados_imovel = await self.api_service.get_imovel_info(
                    inscricao=inscricao_clean
                )
                logger.debug(dados_imovel)
                # Validação passou - salva a inscrição
                state.data["inscricao_imobiliaria"] = inscricao_clean
                logger.info(f"✅ Inscrição salva: {inscricao_clean}")

                if dados_imovel:
                    state.data["endereco"] = dados_imovel["endereco"]
                    state.data["proprietario"] = dados_imovel["proprietario"]
                    logger.info(
                        f"✅ Dados do imóvel carregados - Proprietário: {dados_imovel['proprietario'][:30]}..."
                    )
                else:
                    # Não encontrou dados mas inscrição é válida
                    state.data["endereco"] = None
                    state.data["proprietario"] = None

            except InvalidInscricaoError:
                # Inscrição inválida (código 033) - NÃO salva no state
                logger.warning(f"❌ Inscrição inválida rejeitada: {inscricao_clean}")
                response = AgentResponse(
                    description=IPTUMessageTemplates.solicitar_inscricao(),
                    payload_schema=InscricaoImobiliariaPayload.model_json_schema(),
                    error_message=f"Inscrição {inscricao_clean} não foi encontrada. Por favor, verifique se a inscrição está correta.",
                )
                state.agent_response = response
                return state

            except (APIUnavailableError, AuthenticationError) as e:
                # Se falhar ao buscar dados do imóvel por erro de API, salva a inscrição mas continua sem dados
                logger.warning(f"Não foi possível carregar dados do imóvel: {str(e)}")
                state.data["inscricao_imobiliaria"] = inscricao_clean
                state.data["endereco"] = None
                state.data["proprietario"] = None

            state.agent_response = None

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
        endereco = state.data.get("endereco")
        proprietario = state.data.get("proprietario")
        if "ano_exercicio" in state.payload:
            validated_data = EscolhaAnoPayload.model_validate(state.payload)
            state.data["ano_exercicio"] = validated_data.ano_exercicio
            state.agent_response = None
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

        inscricao = state.data.get("inscricao_imobiliaria", "")
        exercicio = state.data.get("ano_exercicio", "")
        divida_ativa_info = None
        try:
            logger.info(f"Consultando dívida ativa para inscrição {inscricao}")
            divida_ativa_info = await self.api_service.get_divida_ativa_info(inscricao)

            # Se encontrou dívida ativa, informa ao usuário
            if divida_ativa_info and divida_ativa_info.tem_divida_ativa:
                logger.info(
                    f"Dívida ativa encontrada para inscrição {inscricao}: {len(divida_ativa_info.cdas)} CDAs, {len(divida_ativa_info.efs)} EFs, {len(divida_ativa_info.parcelamentos)} parcelamentos"
                )
                # Salva dados da dívida ativa
                state.data["divida_ativa_data"] = divida_ativa_info.model_dump()

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

            # Se não encontrou dívida ativa ou houve erro, retorna mensagem padrão
            # Remove apenas o ano para permitir nova tentativa
            state.data.pop("ano_exercicio", None)
            state.payload.pop(
                "ano_exercicio", None
            )  # Remove do payload também para evitar loop
            state.internal.pop(STATE_HAS_CONSULTED_GUIAS, None)

            state.agent_response = AgentResponse(
                description=IPTUMessageTemplates.nenhuma_guia_encontrada(
                    inscricao=inscricao,
                    exercicio=exercicio,
                    divida_ativa_info=divida_ativa_info,
                ),
            )
            return state

        # Se chegou aqui, encontrou guias
        state.data["dados_guias"] = dados_guias.model_dump()
        state.internal[STATE_HAS_CONSULTED_GUIAS] = True

        # Remove rastros de tentativas
        key_tentativas = f"{STATE_FAILED_ATTEMPTS_PREFIX}{inscricao}"
        state.internal.pop(key_tentativas, None)

        guias_em_aberto = [g for g in dados_guias.guias if g.esta_em_aberto]
        if len(guias_em_aberto) == 0:
            logger.debug(f"Guias em aberto encontradas: {guias_em_aberto}")
            guias_info = self._buscar_guias_detalhadas(state)

            state.agent_response = AgentResponse(
                description=guias_info,
            )
            return state

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
                guias_info = self._buscar_guias_detalhadas(state)
                state.agent_response = AgentResponse(
                    description=guias_info,
                    payload_schema=EscolhaGuiasIPTUPayload.model_json_schema(),
                    error_message=f"Seleção inválida: {str(e)}",
                )
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
            divida_ativa_info=state.data.get("divida_ativa_data", None),
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
            cotas_escolhidas = validated_data.cotas_escolhidas

            # Validação: Verifica se alguma cota escolhida está paga
            dados_cotas_dict = state.data.get("dados_cotas")
            if dados_cotas_dict:
                dados_cotas = DadosCotas(**dados_cotas_dict)

                # Cria um mapa de número_cota -> esta_paga
                cotas_map = {c.numero_cota: c.esta_paga for c in dados_cotas.cotas}

                # Verifica se há cotas pagas na seleção
                cotas_pagas_selecionadas = [
                    cota for cota in cotas_escolhidas if cotas_map.get(cota, False)
                ]

                if cotas_pagas_selecionadas:
                    # Usuário tentou selecionar cotas pagas - retorna erro
                    state.agent_response = AgentResponse(
                        description=IPTUMessageTemplates.cotas_pagas_selecionadas(
                            cotas_pagas_selecionadas
                        ),
                        payload_schema=EscolhaCotasParceladasPayload.model_json_schema(),
                    )
                    return state

                # Validação: Se selecionou apenas 1 cota com vencimento em 2026 ou depois
                if len(cotas_escolhidas) == 1:
                    from datetime import datetime
                    
                    # Cria um mapa de número_cota -> data_vencimento
                    cotas_vencimento_map = {c.numero_cota: c.data_vencimento for c in dados_cotas.cotas}
                    
                    cota_selecionada = cotas_escolhidas[0]
                    data_vencimento_str = cotas_vencimento_map.get(cota_selecionada, "")
                    
                    if data_vencimento_str:
                        try:
                            # Parse da data no formato DD/MM/YYYY
                            data_vencimento = datetime.strptime(data_vencimento_str, "%d/%m/%Y")
                            data_limite = datetime(2026, 1, 1)
                            
                            if data_vencimento >= data_limite:
                                # Cota única com vencimento em 2026 ou depois - inválido
                                state.agent_response = AgentResponse(
                                    description=(
                                        "❌ Não é possível selecionar apenas uma cota com vencimento em 2026 ou posterior.\n\n"
                                        "Por favor:\n"
                                        "• Selecione mais cotas, ou\n"
                                        "• Escolha uma cota com vencimento anterior a 2026\n\n"
                                        "Alternativamente, para gerar uma única cota com vencimento em 2026, acesse: https://pref.rio/"
                                    ),
                                    payload_schema=EscolhaCotasParceladasPayload.model_json_schema(),
                                    error_message=f"Cota única com vencimento em {data_vencimento_str} não permitida.",
                                )
                                return state
                        except ValueError:
                            # Se não conseguir parsear a data, apenas loga e continua
                            logger.warning(f"Não foi possível parsear data de vencimento: {data_vencimento_str}")

            state.data["cotas_escolhidas"] = cotas_escolhidas
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

        # Prepara dados dos boletos para exibição
        inscricao = state.data.get("inscricao_imobiliaria", "N/A")
        boletos_formatados = utils.preparar_dados_boletos_para_template(guias_geradas)

        # Mensagem de sucesso com os boletos gerados
        mensagem_final = IPTUMessageTemplates.boletos_gerados_finalizacao(
            boletos_formatados, inscricao
        )

        # Define resposta final
        state.agent_response = AgentResponse(
            service_name=self.service_name,
            description=mensagem_final,
            payload_schema=None,  # Sem schema - permite qualquer pergunta ou nova inscrição
            data={"guias_geradas": guias_geradas},
        )

        # Reset completo do estado para permitir nova consulta
        state_helpers.reset_completo(state)

        return state

    def _gerar_descricao_boletos_gerados(self, state: ServiceState) -> str:
        """Gera a descrição padrão dos boletos gerados."""
        guias_geradas = state.data.get("guias_geradas", [])
        inscricao = state.data.get("inscricao_imobiliaria", "N/A")

        # Prepara dados dos boletos para o template
        boletos_formatados = utils.preparar_dados_boletos_para_template(guias_geradas)

        return IPTUMessageTemplates.boletos_gerados_finalizacao(
            boletos_formatados, inscricao
        )

    # --- Roteadores Condicionais ---

    def _decide_after_data_collection(self, state: ServiceState):
        """Roteador genérico para nós de coleta de dados."""
        if state.agent_response is not None:
            return END
        return "continue"

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
        if state.agent_response is not None:
            return END
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

        # Após gerar DARM, sempre finaliza (com mensagem de sucesso e reset automático)
        graph.add_edge("gerar_darm", END)

        return graph
