"""
Workflow Dívida Ativa - Prefeitura do Rio de Janeiro

Implementa o início do fluxo de consulta de dívidas ativas.
"""

from langgraph.graph import StateGraph, END
from pydantic import ValidationError

from src.tools.multi_step_service.core import (
    AgentResponse,
    BaseWorkflow,
    ServiceState,
    handle_errors,
)
from src.tools.multi_step_service.workflows.divida_ativa.core.models import (
    AcaoResultadoConsultaPayload,
    AcaoPagamentoRecusadoPayload,
    AutoInfracaoPayload,
    CdaPayload,
    ConfirmacaoPagamentoAVistaPayload,
    DebitosEscolhidosPayload,
    CpfCnpjPayload,
    ExecucaoFiscalPayload,
    FormaPagamentoAVistaPayload,
    InscricaoImobiliariaPayload,
    MenuPagamentoCompletoPayload,
    MenuPagamentoParceladoPayload,
    OpcaoPagarAVistaPayload,
    TipoConsultaPayload,
)
from src.tools.multi_step_service.workflows.divida_ativa.core.constants import (
    STATE_CONSULTA_REALIZADA,
    STATE_CURRENT_VIEW,
    STATE_DIVIDA_ATIVA_CACHE,
    STATE_PAYLOAD_ESPERADO,
    STATE_TIPO_CONSULTA_CACHE,
)
from src.tools.multi_step_service.workflows.divida_ativa.templates import (
    DividaAtivaTemplates,
)


class DividaAtivaWorkflow(BaseWorkflow):
    """
    Workflow para consulta de Dívida Ativa da Prefeitura do Rio.

    Fluxo:
    1. Envia WhatsApp Flow para escolha do tipo de consulta
    2. Salva a escolha para os próximos passos
    """

    service_name = "divida_ativa"
    description = "Consulta de dívidas ativas junto à Prefeitura do Rio de Janeiro."
    automatic_resets = True
    step_order = [
        "tipo_consulta",
        "cpf_cnpj",
        "inscricao_imobiliaria",
        "ano_auto_infracao",
        "numero_auto_infracao",
        "cda",
        "execucao_fiscal",
    ]
    step_dependencies = {
        "tipo_consulta": [
            "cpf_cnpj",
            "inscricao_imobiliaria",
            "ano_auto_infracao",
            "numero_auto_infracao",
            "cda",
            "execucao_fiscal",
            "divida_ativa",
            "acao_resultado",
            "opcao_menu",
            "opcao_pagar_a_vista",
            "debitos_escolhidos",
            "confirmar_pagamento_a_vista",
            "forma_pagamento_a_vista",
        ],
        "cpf_cnpj": [
            "divida_ativa",
            "acao_resultado",
            "opcao_menu",
            "opcao_pagar_a_vista",
            "debitos_escolhidos",
            "confirmar_pagamento_a_vista",
            "forma_pagamento_a_vista",
        ],
        "inscricao_imobiliaria": [
            "divida_ativa",
            "acao_resultado",
            "opcao_menu",
            "opcao_pagar_a_vista",
            "debitos_escolhidos",
            "confirmar_pagamento_a_vista",
            "forma_pagamento_a_vista",
        ],
        "ano_auto_infracao": [
            "numero_auto_infracao",
            "divida_ativa",
            "acao_resultado",
            "opcao_menu",
            "opcao_pagar_a_vista",
            "debitos_escolhidos",
            "confirmar_pagamento_a_vista",
            "forma_pagamento_a_vista",
        ],
        "numero_auto_infracao": [
            "divida_ativa",
            "acao_resultado",
            "opcao_menu",
            "opcao_pagar_a_vista",
            "debitos_escolhidos",
            "confirmar_pagamento_a_vista",
            "forma_pagamento_a_vista",
        ],
        "cda": [
            "divida_ativa",
            "acao_resultado",
            "opcao_menu",
            "opcao_pagar_a_vista",
            "debitos_escolhidos",
            "confirmar_pagamento_a_vista",
            "forma_pagamento_a_vista",
        ],
        "execucao_fiscal": [
            "divida_ativa",
            "acao_resultado",
            "opcao_menu",
            "opcao_pagar_a_vista",
            "debitos_escolhidos",
            "confirmar_pagamento_a_vista",
            "forma_pagamento_a_vista",
        ],
        "acao_resultado": [
            "opcao_menu",
            "opcao_pagar_a_vista",
            "debitos_escolhidos",
            "confirmar_pagamento_a_vista",
            "forma_pagamento_a_vista",
        ],
        "opcao_menu": [
            "opcao_pagar_a_vista",
            "debitos_escolhidos",
            "confirmar_pagamento_a_vista",
            "forma_pagamento_a_vista",
        ],
        "opcao_pagar_a_vista": [
            "debitos_escolhidos",
            "confirmar_pagamento_a_vista",
            "forma_pagamento_a_vista",
        ],
        "debitos_escolhidos": [
            "confirmar_pagamento_a_vista",
            "forma_pagamento_a_vista",
        ],
        "confirmar_pagamento_a_vista": ["forma_pagamento_a_vista"],
        "forma_pagamento_a_vista": [],
    }
    VIEW_TIPO_CONSULTA = "tipo_consulta"
    VIEW_ACAO_RESULTADO = "acao_resultado"
    VIEW_OPCAO_MENU = "opcao_menu"
    VIEW_OPCAO_PAGAR_A_VISTA = "opcao_pagar_a_vista"
    VIEW_DEBITOS_ESCOLHIDOS = "debitos_escolhidos"
    VIEW_CONFIRMAR_PAGAMENTO_A_VISTA = "confirmar_pagamento_a_vista"
    VIEW_FORMA_PAGAMENTO_A_VISTA = "forma_pagamento_a_vista"
    VIEW_ACAO_PAGAMENTO_RECUSADO = "acao_pagamento_recusado"
    view_requirements = {
        VIEW_TIPO_CONSULTA: [],
        VIEW_ACAO_RESULTADO: ["divida_ativa"],
        VIEW_OPCAO_MENU: ["divida_ativa"],
        VIEW_OPCAO_PAGAR_A_VISTA: ["divida_ativa"],
        VIEW_DEBITOS_ESCOLHIDOS: ["divida_ativa"],
        VIEW_CONFIRMAR_PAGAMENTO_A_VISTA: [
            "divida_ativa",
            "debitos_pagamento_a_vista",
        ],
        VIEW_FORMA_PAGAMENTO_A_VISTA: [
            "divida_ativa",
            "debitos_pagamento_a_vista",
        ],
        VIEW_ACAO_PAGAMENTO_RECUSADO: ["divida_ativa"],
    }
    view_back_targets = {
        VIEW_OPCAO_MENU: VIEW_ACAO_RESULTADO,
        VIEW_OPCAO_PAGAR_A_VISTA: VIEW_OPCAO_MENU,
        VIEW_DEBITOS_ESCOLHIDOS: VIEW_OPCAO_PAGAR_A_VISTA,
        VIEW_CONFIRMAR_PAGAMENTO_A_VISTA: VIEW_OPCAO_PAGAR_A_VISTA,
        VIEW_FORMA_PAGAMENTO_A_VISTA: VIEW_CONFIRMAR_PAGAMENTO_A_VISTA,
        VIEW_ACAO_PAGAMENTO_RECUSADO: VIEW_CONFIRMAR_PAGAMENTO_A_VISTA,
    }
    view_dependencies = {
        VIEW_ACAO_RESULTADO: {
            "data": [
                "acao_resultado",
                "opcao_menu",
                "opcao_pagar_a_vista",
                "debitos_escolhidos",
                "confirmar_pagamento_a_vista",
                "forma_pagamento_a_vista",
            ],
            "divida_ativa": [
                "opcao_menu_selecionada",
                "opcao_pagar_a_vista",
                "debitos_pagamento_a_vista",
                "debitos_pagamento_a_vista_labels",
                "confirmar_pagamento_a_vista",
                "forma_pagamento_a_vista",
                "guia_pagamento_a_vista",
                "acao_pagamento_recusado",
                "renderizacao_menu",
            ],
        },
        VIEW_OPCAO_MENU: {
            "data": [
                "opcao_menu",
                "opcao_pagar_a_vista",
                "debitos_escolhidos",
                "confirmar_pagamento_a_vista",
                "forma_pagamento_a_vista",
            ],
            "divida_ativa": [
                "opcao_menu_selecionada",
                "opcao_pagar_a_vista",
                "debitos_pagamento_a_vista",
                "debitos_pagamento_a_vista_labels",
                "confirmar_pagamento_a_vista",
                "forma_pagamento_a_vista",
                "guia_pagamento_a_vista",
                "acao_pagamento_recusado",
                "renderizacao_menu",
            ],
        },
        VIEW_OPCAO_PAGAR_A_VISTA: {
            "data": [
                "opcao_pagar_a_vista",
                "debitos_escolhidos",
                "confirmar_pagamento_a_vista",
                "forma_pagamento_a_vista",
            ],
            "divida_ativa": [
                "opcao_pagar_a_vista",
                "debitos_pagamento_a_vista",
                "debitos_pagamento_a_vista_labels",
                "confirmar_pagamento_a_vista",
                "forma_pagamento_a_vista",
                "guia_pagamento_a_vista",
                "acao_pagamento_recusado",
            ],
        },
        VIEW_DEBITOS_ESCOLHIDOS: {
            "data": [
                "debitos_escolhidos",
                "confirmar_pagamento_a_vista",
                "forma_pagamento_a_vista",
            ],
            "divida_ativa": [
                "debitos_pagamento_a_vista",
                "debitos_pagamento_a_vista_labels",
                "confirmar_pagamento_a_vista",
                "forma_pagamento_a_vista",
                "guia_pagamento_a_vista",
                "acao_pagamento_recusado",
            ],
        },
        VIEW_CONFIRMAR_PAGAMENTO_A_VISTA: {
            "data": ["confirmar_pagamento_a_vista", "forma_pagamento_a_vista"],
            "divida_ativa": [
                "confirmar_pagamento_a_vista",
                "forma_pagamento_a_vista",
                "guia_pagamento_a_vista",
                "acao_pagamento_recusado",
            ],
        },
        VIEW_FORMA_PAGAMENTO_A_VISTA: {
            "data": ["forma_pagamento_a_vista"],
            "divida_ativa": ["forma_pagamento_a_vista", "guia_pagamento_a_vista"],
        },
    }
    tipo_consulta_steps = {
        "cpf_cnpj": "consultar_cpf_cnpj",
        "inscricao_imobiliaria": "consultar_inscricao_imobiliaria",
        "auto_infracao": "consultar_auto_infracao",
        "cda": "consultar_cda",
        "execucao_fiscal": "consultar_execucao_fiscal",
    }
    tipo_consulta_payload_fields = {
        "cpf_cnpj": "cpf_cnpj",
        "inscricao_imobiliaria": "inscricao_imobiliaria",
        "auto_infracao": "numero_auto_infracao",
        "cda": "cda",
        "execucao_fiscal": "execucao_fiscal",
    }
    opcoes_menu_completo = [
        "pagar_a_vista",
        "parcelar_debitos",
        "regularizar_debitos",
        "liquidar_parcelamento",
        "emitir_2_via",
        "voltar",
    ]
    opcoes_menu_nao_parcelado = [
        "pagar_a_vista",
        "parcelar_debitos",
        "voltar",
    ]
    opcoes_menu_parcelado = [
        "parcelar_debitos",
        "regularizar_debitos",
        "liquidar_parcelamento",
        "emitir_2_via",
        "voltar",
    ]

    _api_service = None
    divida_ativa_public_fields = {
        "tipo_consulta",
        "valor_consulta",
        "total_nao_parcelado",
        "total_parcelado",
        "saldo_total_divida",
        "saldo_total_nao_parcelado",
        "saldo_total_parcelado",
        "data_vencimento",
        "endereco_imovel",
        "bairro_imovel",
        "opcoes_menu",
    }

    @property
    def api_service(self):
        """
        Cria o serviço de API apenas quando o identificador já foi coletado.
        """
        if self._api_service is None:
            from src.tools.multi_step_service.workflows.divida_ativa.api_service import (
                DividaAtivaAPIService,
            )

            self._api_service = DividaAtivaAPIService(user_id=self._user_id)
        return self._api_service

    async def execute(self, state: ServiceState, payload: dict) -> ServiceState:
        automatic_resets = self.automatic_resets
        if self._payload_reconsulta_identica(state, payload or {}):
            self.automatic_resets = False
        try:
            final_state = await super().execute(state, payload)
        finally:
            self.automatic_resets = automatic_resets

        if final_state.agent_response is not None:
            final_state.agent_response = final_state.agent_response.model_copy(
                update={"data": self._build_agent_response_data(final_state)}
            )
            if self._is_final_response(final_state):
                final_state.data = {}
                final_state.internal = {}
        return final_state

    def _is_final_response(self, state: ServiceState) -> bool:
        return bool(
            state.status == "completed"
            and state.agent_response
            and not state.agent_response.payload_schema
        )

    def _build_agent_response_data(self, state: ServiceState) -> dict:
        response_data = self._build_public_response_data(state)
        if self._is_final_response(state):
            response_data.update(state.agent_response.data or {})
        return response_data

    def _build_public_response_data(self, state: ServiceState) -> dict:
        divida_ativa = self._get_public_divida_ativa(state)
        divida_ativa_cache = self._get_divida_ativa_cache(state)
        response = state.agent_response
        schema = response.payload_schema if response else None
        esperado = state.internal.get(STATE_PAYLOAD_ESPERADO) or {}
        current_view = state.internal.get(STATE_CURRENT_VIEW)

        data = {
            "status": state.status,
        }

        if current_view:
            data["current_view"] = current_view

        consulta = self._build_public_consulta_data(divida_ativa)
        if consulta:
            data["consulta"] = consulta

        guia = self._build_public_guia_data(
            divida_ativa_cache.get("guia_pagamento_a_vista")
        )
        if guia:
            data["guia_pagamento_a_vista"] = guia

        proximo_payload = self._build_public_expected_payload_data(schema, esperado)
        if proximo_payload:
            data["proximo_payload"] = proximo_payload

        navegacao = self._build_public_navigation_data(state, schema, current_view)
        if navegacao:
            data["navegacao"] = navegacao

        return data

    def _get_divida_ativa_cache(self, state: ServiceState) -> dict:
        return state.internal.get(STATE_DIVIDA_ATIVA_CACHE, {})

    def _get_public_divida_ativa(self, state: ServiceState) -> dict:
        return state.data.get("divida_ativa", {})

    def _ensure_public_divida_ativa(self, state: ServiceState) -> dict:
        return state.data.setdefault("divida_ativa", {})

    def _ensure_divida_ativa_cache(self, state: ServiceState) -> dict:
        return state.internal.setdefault(STATE_DIVIDA_ATIVA_CACHE, {})

    def _get_divida_ativa_context(self, state: ServiceState) -> dict:
        context = dict(self._get_divida_ativa_cache(state))
        context.update(self._get_public_divida_ativa(state))
        return context

    def _set_divida_ativa_context(
        self, state: ServiceState, divida_ativa: dict
    ) -> None:
        state.data["divida_ativa"] = {
            key: value
            for key, value in divida_ativa.items()
            if key in self.divida_ativa_public_fields and value not in (None, "", [])
        }
        state.internal[STATE_DIVIDA_ATIVA_CACHE] = {
            key: value
            for key, value in divida_ativa.items()
            if key not in self.divida_ativa_public_fields
            and value not in (None, "", [])
        }

    def _payload_reconsulta_identica(
        self,
        state: ServiceState,
        payload: dict,
    ) -> bool:
        divida_ativa = self._get_public_divida_ativa(state)
        tipo_consulta = divida_ativa.get("tipo_consulta")
        if "tipo_consulta" in payload:
            return False

        if not (
            payload
            and tipo_consulta
            and state.internal.get(STATE_CONSULTA_REALIZADA)
            and self._get_divida_ativa_cache(state)
        ):
            return False

        if tipo_consulta == "auto_infracao":
            ano = payload.get("ano_auto_infracao", state.data.get("ano_auto_infracao"))
            numero = payload.get(
                "numero_auto_infracao",
                state.data.get("numero_auto_infracao"),
            )
            if not (ano and numero):
                return False
            valor_consulta = f"{numero} {ano}"
        else:
            campo = self.tipo_consulta_payload_fields.get(tipo_consulta)
            if not campo or campo not in payload:
                return False
            valor_consulta = str(payload[campo])

        return (
            divida_ativa.get("valor_consulta") == valor_consulta
            and divida_ativa.get("tipo_consulta") == tipo_consulta
        )

    def _build_public_consulta_data(self, divida_ativa: dict) -> dict:
        if not divida_ativa:
            return {}

        fields = [
            "tipo_consulta",
            "valor_consulta",
            "total_nao_parcelado",
            "total_parcelado",
            "saldo_total_divida",
            "saldo_total_nao_parcelado",
            "saldo_total_parcelado",
            "data_vencimento",
            "endereco_imovel",
            "bairro_imovel",
            "opcoes_menu",
            "renderizacao_menu",
            "opcao_menu_selecionada",
            "opcao_pagar_a_vista",
            "confirmar_pagamento_a_vista",
            "forma_pagamento_a_vista",
        ]
        consulta = {
            field: divida_ativa[field]
            for field in fields
            if divida_ativa.get(field) not in (None, "", [])
        }

        labels = divida_ativa.get("debitos_pagamento_a_vista_labels")
        if labels:
            consulta["debitos_pagamento_a_vista"] = labels

        return consulta

    # Campos de uma guia, com os nomes alternativos vindos direto da PGM.
    GUIA_CAMPOS = {
        "data_vencimento": ("data_vencimento", "dataVencimento"),
        "link": ("link", "pdf", "arquivoBase64"),
        "codigo_de_barras": ("codigo_de_barras", "codigoDeBarras"),
        "pix": ("pix", "codigoQrEMVPix"),
    }

    def _normalizar_guia(self, guia: dict) -> dict:
        normalizada = {}
        for public_field, candidates in self.GUIA_CAMPOS.items():
            value = next(
                (
                    guia.get(candidate)
                    for candidate in candidates
                    if guia.get(candidate)
                ),
                None,
            )
            if value:
                normalizada[public_field] = value
        return normalizada

    def _guias_da_resposta(self, guia: dict | None) -> list[dict]:
        """
        Todas as guias emitidas na resposta da tool.

        O EPGM emite uma guia por natureza de débito, então a escolha do
        cidadão pode gerar N guias (CHATR-164). 'guias_emitidas' é a fonte; o
        fallback para os campos no topo cobre a resposta de guia única que
        pode estar em cache de conversa.
        """
        if not isinstance(guia, dict):
            return []

        emitidas = guia.get("guias_emitidas")
        if isinstance(emitidas, list):
            normalizadas = [
                self._normalizar_guia(item)
                for item in emitidas
                if isinstance(item, dict)
            ]
            normalizadas = [item for item in normalizadas if item]
            if normalizadas:
                return normalizadas

        unica = self._normalizar_guia(guia)
        return [unica] if unica else []

    def _build_public_guia_data(self, guia: dict | None) -> dict:
        if not isinstance(guia, dict) or not guia.get("api_resposta_sucesso"):
            return {}

        guias = self._guias_da_resposta(guia)
        if not guias:
            return {}

        # Os campos no topo seguem sendo a primeira guia, para quem ainda lê
        # uma só; 'guias' é o que garante que nenhuma se perca.
        public_guia = dict(guias[0])
        public_guia["guias"] = guias
        public_guia["total_guias"] = len(guias)

        return public_guia

    def _build_public_expected_payload_data(
        self,
        schema: dict | None,
        esperado: dict,
    ) -> dict:
        if not schema:
            return {}

        fields = esperado.get("fields") or schema.get("required") or []
        properties = schema.get("properties", {})
        render = schema.get("x-render") or self._schema_field_render(properties)
        field_name = fields[0] if len(fields) == 1 else None
        field_schema = properties.get(field_name, {}) if field_name else {}

        data = {
            "campos": fields,
        }
        if render:
            data["renderizacao"] = render
        if field_name:
            data["campo_principal"] = field_name

        options = self._compact_schema_options(field_schema)
        if options:
            data["opcoes"] = options

        return data

    def _schema_field_render(self, properties: dict) -> str | None:
        for field_schema in properties.values():
            render = field_schema.get("x-render")
            if render:
                return render
        return None

    def _compact_schema_options(self, field_schema: dict) -> list[dict]:
        options = field_schema.get("options") or []
        compact_options = []
        for option in options:
            compact_option = {
                "value": option.get("value"),
                "label": option.get("label"),
            }
            compact_options.append(
                {
                    key: value
                    for key, value in compact_option.items()
                    if value not in (None, "")
                }
            )
        return compact_options

    def _find_back_payload(self, schema: dict | None) -> dict:
        if not schema:
            return {}

        for field_name, field_schema in schema.get("properties", {}).items():
            enum_values = field_schema.get("enum") or []
            option_values = [
                option.get("value")
                for option in field_schema.get("options", [])
                if option.get("value")
            ]
            if "voltar" in enum_values or "voltar" in option_values:
                return {field_name: "voltar"}

        return {}

    def _build_public_navigation_data(
        self,
        state: ServiceState,
        schema: dict | None,
        current_view: str | None,
    ) -> dict:
        navegacao = {}

        back_payload = self._find_back_payload(schema)
        if back_payload and current_view in self.view_back_targets:
            navegacao["pode_voltar"] = True
            navegacao["payload_voltar"] = back_payload
            navegacao["volta_para"] = self.view_back_targets[current_view]
        elif current_view:
            navegacao["pode_voltar"] = False

        correcoes = []
        if state.data.get("tipo_consulta"):
            correcoes.append(
                {
                    "descricao": (
                        "Para trocar o tipo de consulta, envie um novo tipo_consulta."
                    ),
                    "campos": ["tipo_consulta"],
                }
            )

        campos_identificador = [
            campo
            for campo in self._campos_identificador_atual(state)
            if state.data.get(campo)
        ]
        if campos_identificador:
            correcoes.append(
                {
                    "descricao": (
                        "Para corrigir o identificador consultado, envie novamente "
                        "o campo do identificador."
                    ),
                    "campos": campos_identificador,
                }
            )

        if correcoes:
            navegacao["corrigir"] = correcoes

        return navegacao

    def _has_cpf_cnpj_direto(self, state: ServiceState) -> bool:
        """
        Identifica a chamada curta em que o agente já envia CPF/CNPJ no payload.
        """
        return "cpf_cnpj" in state.payload

    def _tipo_consulta_atual(self, state: ServiceState) -> str | None:
        return state.data.get("tipo_consulta") or state.internal.get(
            STATE_TIPO_CONSULTA_CACHE
        )

    def _campo_identificador_atual(self, state: ServiceState) -> str | None:
        tipo_consulta = self._tipo_consulta_atual(state)
        if not tipo_consulta:
            return None
        return self.tipo_consulta_payload_fields.get(tipo_consulta)

    def _campos_identificador_atual(self, state: ServiceState) -> list[str]:
        tipo_consulta = self._tipo_consulta_atual(state)
        if tipo_consulta == "auto_infracao":
            return ["ano_auto_infracao", "numero_auto_infracao"]

        campo_identificador = self._campo_identificador_atual(state)
        return [campo_identificador] if campo_identificador else []

    def _payload_tem_navegacao_suportada(self, state: ServiceState) -> bool:
        if "tipo_consulta" in state.payload:
            return True

        return self._payload_tem_identificador_atual(state)

    def _payload_tem_identificador_atual(self, state: ServiceState) -> bool:
        return bool(set(self._campos_identificador_atual(state)) & set(state.payload))

    def _payload_eh_voltar(self, state: ServiceState) -> bool:
        return any(value == "voltar" for value in state.payload.values())

    def _tem_divida_consultada(self, state: ServiceState) -> bool:
        return bool(
            state.internal.get(STATE_CONSULTA_REALIZADA)
            and self._get_public_divida_ativa(state)
        )

    def _view_tem_requisitos(self, state: ServiceState, view: str) -> bool:
        divida_ativa = self._get_divida_ativa_context(state)
        for requisito in self.view_requirements.get(view, []):
            if requisito == "divida_ativa":
                if not self._tem_divida_consultada(state):
                    return False
                continue

            if requisito in state.data or requisito in divida_ativa:
                continue

            return False

        return True

    def _limpar_dependencias_view(self, state: ServiceState, view: str) -> None:
        dependencies = self.view_dependencies.get(view, {})
        for campo in dependencies.get("data", []):
            state.data.pop(campo, None)

        divida_ativa = self._get_public_divida_ativa(state)
        divida_ativa_cache = self._get_divida_ativa_cache(state)
        for campo in dependencies.get("divida_ativa", []):
            divida_ativa.pop(campo, None)
            divida_ativa_cache.pop(campo, None)

    def _retornar_step_esperado(self, state: ServiceState) -> None:
        esperado = state.internal.get(STATE_PAYLOAD_ESPERADO) or {}
        payload_schema = (
            esperado.get("payload_schema") or self._build_tipo_consulta_schema()
        )
        error_message = (
            esperado.get("error_message") or DividaAtivaTemplates.input_inesperado()
        )
        state.agent_response = AgentResponse(
            service_name=self.service_name,
            description=error_message,
            payload_schema=payload_schema,
            error_message=error_message,
            data=state.data,
        )

    def _payload_esperado_para_input_inesperado(
        self, state: ServiceState
    ) -> dict | None:
        esperado = state.internal.get(STATE_PAYLOAD_ESPERADO)
        if not esperado or not state.payload:
            return None

        expected_fields = set(esperado.get("fields", []))
        if expected_fields.intersection(state.payload.keys()):
            return None

        if self._payload_tem_navegacao_suportada(state):
            return None

        return esperado

    def _processar_payload_fora_do_step(self, state: ServiceState) -> bool:
        if not self._payload_esperado_para_input_inesperado(state):
            return False

        self._retornar_step_esperado(state)
        return True

    def _ensure_divida_consultada(self, state: ServiceState) -> bool:
        if self._tem_divida_consultada(state):
            return True

        self._limpar_divida_consultada(state)
        self._tipo_consulta_response(state)
        return False

    def _ensure_debitos_a_vista(self, state: ServiceState) -> bool:
        if self._tem_divida_consultada(state) and self._get_divida_ativa_context(
            state
        ).get("debitos_pagamento_a_vista"):
            return True

        self._opcao_pagamento_response(
            state,
            DividaAtivaTemplates.input_inesperado(),
            payload_schema=self._build_opcao_pagar_a_vista_schema(),
        )
        return False

    def _processar_identificador_atual(self, state: ServiceState) -> bool:
        if "tipo_consulta" in state.payload:
            return False

        if not self._payload_tem_identificador_atual(state):
            return False

        state.agent_response = None
        return True

    def _processar_tipo_consulta_payload(self, state: ServiceState) -> bool:
        if "tipo_consulta" not in state.payload:
            return False

        schema = self._build_tipo_consulta_schema()
        try:
            validated = TipoConsultaPayload.model_validate(state.payload)
        except ValidationError:
            state.agent_response = AgentResponse(
                service_name=self.service_name,
                description=DividaAtivaTemplates.opcao_menu_indisponivel(),
                payload_schema=schema,
                error_message=DividaAtivaTemplates.opcao_menu_indisponivel(),
            )
            self._registrar_payload_esperado(
                state,
                schema,
                DividaAtivaTemplates.solicitar_tipo_consulta(),
                DividaAtivaTemplates.opcao_menu_indisponivel(),
            )
            return True

        state.data["tipo_consulta"] = validated.tipo_consulta
        state.internal[STATE_TIPO_CONSULTA_CACHE] = validated.tipo_consulta
        self._limpar_payload_esperado(state)
        state.agent_response = None
        return True

    def _processar_tipo_consulta_cache(self, state: ServiceState) -> bool:
        if not state.internal.get(STATE_TIPO_CONSULTA_CACHE):
            return False

        state.agent_response = None
        return True

    def _processar_cpf_cnpj_direto(self, state: ServiceState) -> bool:
        if not self._has_cpf_cnpj_direto(state):
            return False

        state.data["tipo_consulta"] = "cpf_cnpj"
        state.internal[STATE_TIPO_CONSULTA_CACHE] = "cpf_cnpj"
        state.agent_response = None
        return True

    @handle_errors
    async def _selecionar_tipo_consulta(self, state: ServiceState) -> ServiceState:
        """
        Envia o Flow de tipos de consulta e valida a escolha do usuário.
        """
        if self._processar_voltar(state):
            return state

        if self._processar_payload_fora_do_step(state):
            return state

        if await self._processar_forma_pagamento_a_vista(state):
            return state

        if self._processar_acao_pagamento_recusado(state):
            return state

        if self._processar_confirmacao_pagamento_a_vista(state):
            return state

        if self._processar_debitos_escolhidos(state):
            return state

        if self._processar_opcao_pagar_a_vista(state):
            return state

        if self._processar_opcao_menu(state):
            return state

        if self._processar_acao_resultado(state):
            return state

        if self._processar_payload_inesperado(state):
            return state

        if self._processar_identificador_atual(state):
            return state

        if self._processar_tipo_consulta_payload(state):
            return state

        if self._processar_tipo_consulta_cache(state):
            return state

        if self._processar_cpf_cnpj_direto(state):
            return state

        if state.payload.get("continuar") is True:
            return self._tipo_consulta_response(state)

        return self._tipo_consulta_response(state)

    def _limpar_payload_esperado(self, state: ServiceState) -> None:
        state.internal.pop(STATE_PAYLOAD_ESPERADO, None)
        state.internal.pop(STATE_CURRENT_VIEW, None)

    def _registrar_payload_esperado(
        self,
        state: ServiceState,
        payload_schema: dict | None,
        description: str,
        error_message: str,
        current_view: str | None = None,
    ) -> None:
        if not payload_schema:
            self._limpar_payload_esperado(state)
            return

        properties = payload_schema.get("properties", {})
        required = payload_schema.get("required") or list(properties.keys())
        view = current_view or (required[0] if required else None)
        state.internal[STATE_PAYLOAD_ESPERADO] = {
            "fields": required,
            "description": description,
            "payload_schema": payload_schema,
            "error_message": error_message,
        }
        if view:
            state.internal[STATE_CURRENT_VIEW] = view

    def _processar_payload_inesperado(self, state: ServiceState) -> bool:
        esperado = self._payload_esperado_para_input_inesperado(state)
        if not esperado:
            return False

        state.agent_response = AgentResponse(
            service_name=self.service_name,
            description=esperado.get("error_message")
            or DividaAtivaTemplates.input_inesperado(),
            payload_schema=esperado.get("payload_schema"),
            error_message=esperado.get("error_message")
            or DividaAtivaTemplates.input_inesperado(),
            data=state.data,
        )
        return True

    def _schema_response(
        self, state: ServiceState, description: str, payload_schema: dict
    ) -> ServiceState:
        self._registrar_payload_esperado(
            state,
            payload_schema,
            description,
            DividaAtivaTemplates.input_inesperado(),
        )
        state.agent_response = AgentResponse(
            service_name=self.service_name,
            description=description,
            payload_schema=payload_schema,
        )
        return state

    def _final_response(
        self,
        state: ServiceState,
        description: str,
        data: dict | None = None,
    ) -> ServiceState:
        final_data = data or {}
        state.status = "completed"
        state.data = final_data
        state.internal = {}
        state.agent_response = AgentResponse(
            service_name=self.service_name,
            description=description,
            payload_schema=None,
            data=final_data,
        )
        return state

    def _action_response(self, state: ServiceState, description: str) -> ServiceState:
        schema = self._build_acao_resultado_schema()
        self._registrar_payload_esperado(
            state,
            schema,
            description,
            DividaAtivaTemplates.opcao_botao_indisponivel(),
        )
        state.agent_response = AgentResponse(
            service_name=self.service_name,
            description=description,
            payload_schema=schema,
            data=state.data,
        )
        return state

    def _build_tipo_consulta_schema(self) -> dict:
        schema = TipoConsultaPayload.model_json_schema()
        schema["x-render"] = "list"
        return schema

    def _build_acao_resultado_schema(self) -> dict:
        schema = AcaoResultadoConsultaPayload.model_json_schema()
        schema["x-render"] = "buttons"
        return schema

    def _get_opcoes_menu(self, total_nao_parcelado: int, total_parcelado: int) -> list:
        if total_nao_parcelado > 0 and total_parcelado > 0:
            return self.opcoes_menu_completo
        if total_nao_parcelado > 0 and total_parcelado == 0:
            return self.opcoes_menu_nao_parcelado
        if total_nao_parcelado == 0 and total_parcelado > 0:
            return self.opcoes_menu_parcelado
        return []

    def _get_renderizacao_menu(self, opcoes_menu: list) -> str:
        if opcoes_menu == self.opcoes_menu_nao_parcelado:
            return "buttons"
        return "list"

    def _build_menu_pagamento_schema(self, opcoes_menu: list) -> dict:
        if opcoes_menu == self.opcoes_menu_completo:
            schema = MenuPagamentoCompletoPayload.model_json_schema()
            schema["x-render"] = "list"
            return schema

        if opcoes_menu == self.opcoes_menu_parcelado:
            schema = MenuPagamentoParceladoPayload.model_json_schema()
            schema["x-render"] = "list"
            return schema

        renderizacao = self._get_renderizacao_menu(opcoes_menu)
        return {
            "type": "object",
            "title": "Opções de pagamento",
            "properties": {
                "opcao_menu": {
                    "type": "string",
                    "title": "Opções de pagamento",
                    "description": "Escolha uma opção.",
                    "enum": opcoes_menu,
                    "options": [
                        {
                            "value": opcao,
                            "label": DividaAtivaTemplates.OPCAO_MENU_LABELS.get(
                                opcao, opcao
                            ),
                        }
                        for opcao in opcoes_menu
                    ],
                    "x-render": renderizacao,
                }
            },
            "required": ["opcao_menu"],
            "x-render": renderizacao,
        }

    def _limpar_divida_consultada(self, state: ServiceState) -> None:
        state.data.pop("tipo_consulta", None)
        for campo in self.step_dependencies["tipo_consulta"]:
            state.data.pop(campo, None)
        state.data.pop("divida_ativa", None)
        state.internal.pop(STATE_DIVIDA_ATIVA_CACHE, None)
        state.internal.pop(STATE_CONSULTA_REALIZADA, None)
        state.internal.pop(STATE_TIPO_CONSULTA_CACHE, None)
        self._limpar_payload_esperado(state)

    def _render_view(self, state: ServiceState, view: str) -> ServiceState:
        if not self._view_tem_requisitos(state, view):
            self._limpar_divida_consultada(state)
            return self._tipo_consulta_response(state)

        divida_ativa = self._get_divida_ativa_context(state)

        if view == self.VIEW_ACAO_RESULTADO:
            return self._action_response(
                state,
                divida_ativa.get("mensagem_divida_contribuinte")
                or DividaAtivaTemplates.consulta_realizada(),
            )

        if view == self.VIEW_OPCAO_MENU:
            return self._menu_pagamento_response(
                state,
                DividaAtivaTemplates.escolher_opcao_pagamento(),
            )

        if view == self.VIEW_OPCAO_PAGAR_A_VISTA:
            return self._opcao_pagamento_response(
                state,
                DividaAtivaTemplates.pagar_a_vista(),
                payload_schema=self._build_opcao_pagar_a_vista_schema(),
            )

        if view == self.VIEW_DEBITOS_ESCOLHIDOS:
            return self._debitos_escolhidos_response(state)

        if view == self.VIEW_CONFIRMAR_PAGAMENTO_A_VISTA:
            labels = divida_ativa.get("debitos_pagamento_a_vista_labels", [])
            return self._opcao_pagamento_response(
                state,
                DividaAtivaTemplates.confirmar_pagamento_a_vista(labels),
                payload_schema=self._build_confirmacao_pagamento_schema(),
            )

        if view == self.VIEW_FORMA_PAGAMENTO_A_VISTA:
            return self._opcao_pagamento_response(
                state,
                DividaAtivaTemplates.pagamento_a_vista_confirmado(),
                payload_schema=self._build_forma_pagamento_schema(),
            )

        return self._tipo_consulta_response(state)

    def _voltar_para_view_response(
        self, state: ServiceState, target_view: str
    ) -> ServiceState:
        self._limpar_dependencias_view(state, target_view)
        return self._render_view(state, target_view)

    def _processar_voltar(self, state: ServiceState) -> bool:
        if not self._payload_eh_voltar(state):
            return False

        current_view = state.internal.get(STATE_CURRENT_VIEW)
        target_view = self.view_back_targets.get(current_view)
        if not target_view:
            return False

        self._voltar_para_view_response(state, target_view)
        return True

    def _tipo_consulta_response(self, state: ServiceState) -> ServiceState:
        schema = self._build_tipo_consulta_schema()
        self._registrar_payload_esperado(
            state,
            schema,
            DividaAtivaTemplates.solicitar_tipo_consulta(),
            DividaAtivaTemplates.opcao_menu_indisponivel(),
        )
        state.agent_response = AgentResponse(
            service_name=self.service_name,
            description=DividaAtivaTemplates.solicitar_tipo_consulta(),
            payload_schema=schema,
            data=state.data,
        )
        return state

    def _menu_pagamento_response(
        self,
        state: ServiceState,
        description: str,
        error_message: str | None = None,
    ) -> ServiceState:
        divida_ativa = self._get_divida_ativa_context(state)
        opcoes_menu = divida_ativa.get("opcoes_menu", [])
        schema = self._build_menu_pagamento_schema(opcoes_menu)
        self._registrar_payload_esperado(
            state,
            schema,
            description,
            error_message or DividaAtivaTemplates.opcao_menu_indisponivel(),
        )
        self._ensure_public_divida_ativa(state)["renderizacao_menu"] = schema[
            "x-render"
        ]
        state.agent_response = AgentResponse(
            service_name=self.service_name,
            description=description,
            payload_schema=schema,
            error_message=error_message,
            data=state.data,
        )
        return state

    def _opcao_pagamento_response(
        self,
        state: ServiceState,
        description: str,
        payload_schema: dict | None = None,
    ) -> ServiceState:
        if payload_schema:
            error_message = DividaAtivaTemplates.opcao_botao_indisponivel()
            if "debitos_escolhidos" in payload_schema.get("properties", {}):
                total_debitos = len(self._get_debitos_pagaveis_a_vista(state))
                error_message = DividaAtivaTemplates.debitos_escolhidos_invalidos(
                    total_debitos
                )
            self._registrar_payload_esperado(
                state,
                payload_schema,
                description,
                error_message,
            )
        else:
            self._limpar_payload_esperado(state)

        state.agent_response = AgentResponse(
            service_name=self.service_name,
            description=description,
            payload_schema=payload_schema,
            data=state.data,
        )
        return state

    def _build_opcao_pagar_a_vista_schema(self) -> dict:
        schema = OpcaoPagarAVistaPayload.model_json_schema()
        schema["x-render"] = "buttons"
        return schema

    def _build_confirmacao_pagamento_schema(self) -> dict:
        schema = ConfirmacaoPagamentoAVistaPayload.model_json_schema()
        schema["x-render"] = "buttons"
        return schema

    def _build_forma_pagamento_schema(self) -> dict:
        schema = FormaPagamentoAVistaPayload.model_json_schema()
        schema["x-render"] = "buttons"
        return schema

    def _build_acao_pagamento_recusado_schema(self) -> dict:
        schema = AcaoPagamentoRecusadoPayload.model_json_schema()
        schema["x-render"] = "buttons"
        return schema

    def _get_debitos_pagaveis_a_vista(self, state: ServiceState) -> list[dict]:
        divida_ativa = self._get_divida_ativa_context(state)
        debitos = []

        for cda in divida_ativa.get("lista_cdas", []):
            debitos.append({"tipo": "cda", "identificador": cda})

        for ef in divida_ativa.get("lista_efs", []):
            debitos.append({"tipo": "execucao_fiscal", "identificador": ef})

        for indice, debito in enumerate(debitos, start=1):
            debito["indice"] = indice
            debito["label"] = f"{indice}. {debito['identificador']}"

        return debitos

    def _get_cdas_efs_para_emissao_a_vista(
        self, state: ServiceState
    ) -> tuple[list[str], list[str]]:
        divida_ativa = self._get_divida_ativa_context(state)
        debitos = divida_ativa.get("debitos_pagamento_a_vista") or []

        cdas = [
            debito["identificador"]
            for debito in debitos
            if debito.get("tipo") == "cda" and debito.get("identificador")
        ]
        efs = [
            debito["identificador"]
            for debito in debitos
            if debito.get("tipo") == "execucao_fiscal" and debito.get("identificador")
        ]

        return cdas, efs

    def _salvar_debitos_a_vista(
        self,
        state: ServiceState,
        debitos: list[dict],
    ) -> None:
        divida_ativa = self._ensure_public_divida_ativa(state)
        divida_ativa_cache = self._ensure_divida_ativa_cache(state)
        divida_ativa_cache["debitos_pagamento_a_vista"] = debitos
        labels = [debito["label"] for debito in debitos]
        divida_ativa["debitos_pagamento_a_vista_labels"] = labels
        divida_ativa_cache["debitos_pagamento_a_vista_labels"] = labels

    def _confirmar_debitos_a_vista_response(
        self,
        state: ServiceState,
        debitos: list[dict],
    ) -> ServiceState:
        self._salvar_debitos_a_vista(state, debitos)
        return self._opcao_pagamento_response(
            state,
            DividaAtivaTemplates.confirmar_pagamento_a_vista(
                [debito["label"] for debito in debitos]
            ),
            payload_schema=self._build_confirmacao_pagamento_schema(),
        )

    def _debitos_escolhidos_response(self, state: ServiceState) -> ServiceState:
        debitos = self._get_debitos_pagaveis_a_vista(state)
        return self._opcao_pagamento_response(
            state,
            DividaAtivaTemplates.escolher_debitos_a_vista(len(debitos)),
            payload_schema=DebitosEscolhidosPayload.model_json_schema(),
        )

    def _parse_indices_debitos(
        self,
        valor: str,
        total_debitos: int,
    ) -> list[int] | None:
        try:
            indices = [int(item.strip()) for item in valor.split(",") if item.strip()]
        except ValueError:
            return None

        if not indices:
            return None

        if any(indice < 1 or indice > total_debitos for indice in indices):
            return None

        return list(dict.fromkeys(indices))

    def _processar_confirmacao_pagamento_a_vista(
        self,
        state: ServiceState,
    ) -> bool:
        if "confirmar_pagamento_a_vista" not in state.payload:
            return False

        if not self._ensure_debitos_a_vista(state):
            return True

        try:
            validated = ConfirmacaoPagamentoAVistaPayload.model_validate(state.payload)
        except ValidationError:
            self._opcao_pagamento_response(
                state,
                DividaAtivaTemplates.opcao_botao_indisponivel(),
                payload_schema=self._build_confirmacao_pagamento_schema(),
            )
            return True

        divida_ativa = self._ensure_public_divida_ativa(state)
        divida_ativa["confirmar_pagamento_a_vista"] = (
            validated.confirmar_pagamento_a_vista
        )
        state.data["confirmar_pagamento_a_vista"] = (
            validated.confirmar_pagamento_a_vista
        )

        if validated.confirmar_pagamento_a_vista == "nao":
            self._opcao_pagamento_response(
                state,
                DividaAtivaTemplates.pagamento_a_vista_recusado(),
                payload_schema=self._build_acao_pagamento_recusado_schema(),
            )
            return True

        self._opcao_pagamento_response(
            state,
            DividaAtivaTemplates.pagamento_a_vista_confirmado(),
            payload_schema=self._build_forma_pagamento_schema(),
        )
        return True

    def _processar_acao_pagamento_recusado(
        self,
        state: ServiceState,
    ) -> bool:
        if "acao_pagamento_recusado" not in state.payload:
            return False

        if not self._ensure_divida_consultada(state):
            return True

        try:
            validated = AcaoPagamentoRecusadoPayload.model_validate(state.payload)
        except ValidationError:
            self._opcao_pagamento_response(
                state,
                DividaAtivaTemplates.opcao_botao_indisponivel(),
                payload_schema=self._build_acao_pagamento_recusado_schema(),
            )
            return True

        divida_ativa = self._ensure_public_divida_ativa(state)
        divida_ativa["acao_pagamento_recusado"] = validated.acao_pagamento_recusado

        if validated.acao_pagamento_recusado == "escolher_debitos":
            self._debitos_escolhidos_response(state)
            return True

        if validated.acao_pagamento_recusado == "opcoes_pagamento":
            self._menu_pagamento_response(
                state,
                DividaAtivaTemplates.escolher_opcao_pagamento(),
            )
            return True

        self._final_response(
            state,
            DividaAtivaTemplates.atendimento_encerrado(),
        )
        return True

    def _formatar_resposta_forma_pagamento_a_vista(
        self,
        forma_pagamento: str,
        guia: dict,
    ) -> str:
        guias = self._guias_da_resposta(guia)

        if forma_pagamento == "boleto_bancario":
            return DividaAtivaTemplates.boleto_bancario_a_vista(guias)

        if forma_pagamento == "codigo_barras":
            return DividaAtivaTemplates.codigo_barras_a_vista(guias)

        return DividaAtivaTemplates.pix_copia_e_cola_a_vista(guias)

    async def _processar_forma_pagamento_a_vista(self, state: ServiceState) -> bool:
        if "forma_pagamento_a_vista" not in state.payload:
            return False

        if not self._ensure_debitos_a_vista(state):
            return True

        try:
            validated = FormaPagamentoAVistaPayload.model_validate(state.payload)
        except ValidationError:
            self._opcao_pagamento_response(
                state,
                DividaAtivaTemplates.opcao_botao_indisponivel(),
                payload_schema=self._build_forma_pagamento_schema(),
            )
            return True

        divida_ativa = self._ensure_public_divida_ativa(state)
        divida_ativa_cache = self._ensure_divida_ativa_cache(state)
        divida_ativa["forma_pagamento_a_vista"] = validated.forma_pagamento_a_vista
        state.data["forma_pagamento_a_vista"] = validated.forma_pagamento_a_vista

        cdas, efs = self._get_cdas_efs_para_emissao_a_vista(state)
        guia = await self.api_service.emitir_guia_a_vista(cdas=cdas, efs=efs)

        divida_ativa_cache["guia_pagamento_a_vista"] = guia

        if not guia or not guia.get("api_resposta_sucesso"):
            erro_guia = guia.get("api_descricao_erro") if guia else None
            self._opcao_pagamento_response(
                state,
                erro_guia or DividaAtivaTemplates.guia_a_vista_nao_emitida(),
            )
            return True

        self._final_response(
            state,
            self._formatar_resposta_forma_pagamento_a_vista(
                validated.forma_pagamento_a_vista,
                guia,
            ),
            data={
                "guia_pagamento_a_vista": self._build_public_guia_data(guia),
            },
        )
        return True

    def _processar_debitos_escolhidos(self, state: ServiceState) -> bool:
        if "debitos_escolhidos" not in state.payload:
            return False

        if not self._ensure_divida_consultada(state):
            return True

        debitos = self._get_debitos_pagaveis_a_vista(state)
        total_debitos = len(debitos)

        try:
            validated = DebitosEscolhidosPayload.model_validate(state.payload)
        except ValidationError:
            self._opcao_pagamento_response(
                state,
                DividaAtivaTemplates.debitos_escolhidos_invalidos(total_debitos),
                payload_schema=DebitosEscolhidosPayload.model_json_schema(),
            )
            return True

        indices = self._parse_indices_debitos(
            validated.debitos_escolhidos,
            total_debitos,
        )
        if indices is None:
            self._opcao_pagamento_response(
                state,
                DividaAtivaTemplates.debitos_escolhidos_invalidos(total_debitos),
                payload_schema=DebitosEscolhidosPayload.model_json_schema(),
            )
            return True

        debitos_selecionados = [debitos[indice - 1] for indice in indices]
        state.data["debitos_escolhidos"] = validated.debitos_escolhidos
        self._confirmar_debitos_a_vista_response(state, debitos_selecionados)
        return True

    def _processar_opcao_pagar_a_vista(self, state: ServiceState) -> bool:
        if "opcao_pagar_a_vista" not in state.payload:
            return False

        if not self._ensure_divida_consultada(state):
            return True

        try:
            validated = OpcaoPagarAVistaPayload.model_validate(state.payload)
        except ValidationError:
            self._opcao_pagamento_response(
                state,
                DividaAtivaTemplates.opcao_botao_indisponivel(),
                payload_schema=self._build_opcao_pagar_a_vista_schema(),
            )
            return True

        divida_ativa = self._ensure_public_divida_ativa(state)
        divida_ativa["opcao_pagar_a_vista"] = validated.opcao_pagar_a_vista
        state.data["opcao_pagar_a_vista"] = validated.opcao_pagar_a_vista

        if validated.opcao_pagar_a_vista == "pagar_tudo":
            debitos = self._get_debitos_pagaveis_a_vista(state)
            self._confirmar_debitos_a_vista_response(state, debitos)
            return True

        self._debitos_escolhidos_response(state)
        return True

    def _regularizar_debitos_response(self, state: ServiceState) -> ServiceState:
        divida_ativa = self._ensure_public_divida_ativa(state)
        divida_ativa_context = self._get_divida_ativa_context(state)
        total_guias = len(
            [guia for guia in divida_ativa_context.get("lista_guias", []) if guia]
        )

        if total_guias <= 1:
            divida_ativa["opcao_pagar_a_vista"] = "pagar_tudo"
            state.data["opcao_pagar_a_vista"] = "pagar_tudo"
            debitos = self._get_debitos_pagaveis_a_vista(state)
            return self._confirmar_debitos_a_vista_response(state, debitos)

        return self._opcao_pagamento_response(
            state,
            DividaAtivaTemplates.regularizar_debitos(),
            payload_schema=self._build_opcao_pagar_a_vista_schema(),
        )

    def _processar_opcao_menu(self, state: ServiceState) -> bool:
        if "opcao_menu" not in state.payload:
            return False

        divida_ativa = self._get_public_divida_ativa(state)
        if not divida_ativa:
            self._limpar_divida_consultada(state)
            self._tipo_consulta_response(state)
            return True

        opcao_menu = state.payload.get("opcao_menu")
        opcoes_menu = divida_ativa.get("opcoes_menu", [])

        if opcao_menu not in opcoes_menu:
            self._menu_pagamento_response(
                state,
                DividaAtivaTemplates.opcao_menu_indisponivel(),
                error_message=DividaAtivaTemplates.opcao_menu_indisponivel(),
            )
            return True

        divida_ativa["opcao_menu_selecionada"] = opcao_menu
        state.data["opcao_menu"] = opcao_menu

        if opcao_menu == "voltar":
            target_view = self.view_back_targets[self.VIEW_OPCAO_MENU]
            self._voltar_para_view_response(state, target_view)
            return True

        if opcao_menu == "pagar_a_vista":
            self._opcao_pagamento_response(
                state,
                DividaAtivaTemplates.pagar_a_vista(),
                payload_schema=self._build_opcao_pagar_a_vista_schema(),
            )
            return True

        if opcao_menu == "parcelar_debitos":
            self._final_response(
                state,
                DividaAtivaTemplates.parcelar_debitos(),
            )
            return True

        if opcao_menu == "regularizar_debitos":
            self._regularizar_debitos_response(state)
            return True

        if opcao_menu == "liquidar_parcelamento":
            self._final_response(
                state,
                DividaAtivaTemplates.liquidar_parcelamento(),
            )
            return True

        if opcao_menu == "emitir_2_via":
            self._final_response(
                state,
                DividaAtivaTemplates.emitir_2_via(),
            )
            return True

        self._menu_pagamento_response(
            state,
            DividaAtivaTemplates.opcao_menu_indisponivel(),
            error_message=DividaAtivaTemplates.opcao_menu_indisponivel(),
        )
        return True

    def _processar_acao_resultado(self, state: ServiceState) -> bool:
        if "acao_resultado" not in state.payload:
            return False

        try:
            validated = AcaoResultadoConsultaPayload.model_validate(state.payload)
        except ValidationError:
            state.agent_response = AgentResponse(
                service_name=self.service_name,
                description=DividaAtivaTemplates.opcao_botao_indisponivel(),
                payload_schema=self._build_acao_resultado_schema(),
                error_message=DividaAtivaTemplates.opcao_botao_indisponivel(),
                data=state.data,
            )
            return True

        if not self._ensure_divida_consultada(state):
            return True

        state.data["acao_resultado"] = validated.acao_resultado

        if validated.acao_resultado == "consultar_outro_debito":
            self._limpar_divida_consultada(state)
            self._tipo_consulta_response(state)
            return True

        self._menu_pagamento_response(
            state,
            DividaAtivaTemplates.escolher_opcao_pagamento(),
        )
        return True

    def _build_valor_consulta(
        self,
        tipo_consulta: str,
        validated,
        value_field: str,
        extra_fields: dict | None = None,
    ) -> str:
        valor = getattr(validated, value_field)
        if tipo_consulta == "auto_infracao":
            ano = getattr(validated, "ano_auto_infracao")
            return f"{valor} {ano}"
        return valor

    def _salvar_auto_infracao_parcial(self, state: ServiceState) -> None:
        ano = state.payload.get("ano_auto_infracao")
        if isinstance(ano, str):
            ano = ano.strip()
            if len(ano) == 4 and ano.isdigit():
                state.data["ano_auto_infracao"] = ano

    def _build_divida_ativa_data(
        self,
        tipo_consulta: str,
        valor_consulta: str,
        divida_info: dict,
        mensagem: str,
    ) -> dict:
        lista_cdas = divida_info.get("lista_cdas", []) or []
        lista_efs = divida_info.get("lista_efs", []) or []
        lista_guias = divida_info.get("lista_guias", []) or []
        total_nao_parcelado = divida_info.get("total_nao_parcelado")
        total_parcelado = divida_info.get("total_parcelado")

        if total_nao_parcelado is None:
            total_nao_parcelado = len([item for item in lista_cdas + lista_efs if item])
        if total_parcelado is None:
            total_parcelado = len([item for item in lista_guias if item])

        return {
            "tipo_consulta": tipo_consulta,
            "valor_consulta": valor_consulta,
            "mensagem_divida_contribuinte": mensagem,
            "total_nao_parcelado": total_nao_parcelado,
            "total_parcelado": total_parcelado,
            "lista_cdas": [item for item in lista_cdas if item],
            "lista_efs": [item for item in lista_efs if item],
            "lista_guias": [item for item in lista_guias if item],
            "dicionario_itens": divida_info.get("dicionario_itens", {}),
            "total_itens_pagamento": divida_info.get("total_itens_pagamento", 0),
            "debitos_msg": divida_info.get("debitos_msg", []),
            "saldo_total_divida": divida_info.get("saldo_total_divida"),
            "saldo_total_nao_parcelado": divida_info.get("saldo_total_nao_parcelado"),
            "saldo_total_parcelado": divida_info.get("saldo_total_parcelado"),
            "data_vencimento": divida_info.get("data_vencimento"),
            "endereco_imovel": divida_info.get("endereco_imovel"),
            "bairro_imovel": divida_info.get("bairro_imovel"),
            "url_pdf": divida_info.get("url_pdf"),
            "opcoes_menu": self._get_opcoes_menu(
                total_nao_parcelado=total_nao_parcelado,
                total_parcelado=total_parcelado,
            ),
        }

    def _consulta_cacheada_corresponde(
        self,
        state: ServiceState,
        tipo_consulta: str,
        valor_consulta: str,
    ) -> bool:
        divida_ativa = self._get_public_divida_ativa(state)
        return bool(
            state.internal.get(STATE_CONSULTA_REALIZADA)
            and divida_ativa
            and self._get_divida_ativa_cache(state)
            and divida_ativa.get("tipo_consulta") == tipo_consulta
            and divida_ativa.get("valor_consulta") == valor_consulta
        )

    async def _consultar_por_identificador(
        self,
        state: ServiceState,
        node_name: str,
        payload_model,
        value_field: str,
        description: str,
        extra_fields: dict | None = None,
    ) -> ServiceState:
        """
        Solicita o identificador esperado pelo nó ou chama a API quando ele chega.
        """
        schema = payload_model.model_json_schema()
        expected_fields = set(schema.get("properties", {}).keys())

        if not expected_fields.intersection(state.payload.keys()):
            return self._schema_response(state, description, schema)

        validation_payload = {
            field: state.data[field] for field in expected_fields if field in state.data
        }
        validation_payload.update(state.payload)

        try:
            validated = payload_model.model_validate(validation_payload)
        except ValidationError as e:
            tipo_consulta = state.internal.get(STATE_TIPO_CONSULTA_CACHE)
            if not tipo_consulta:
                tipo_consulta = state.data.get("tipo_consulta")
            if tipo_consulta == "auto_infracao":
                self._salvar_auto_infracao_parcial(state)
            self._registrar_payload_esperado(
                state,
                schema,
                description,
                str(e),
            )
            state.agent_response = AgentResponse(
                service_name=self.service_name,
                description=description,
                payload_schema=schema,
                error_message=str(e),
            )
            return state

        tipo_consulta = state.internal.get(STATE_TIPO_CONSULTA_CACHE)
        if not tipo_consulta:
            tipo_consulta = state.data.get("tipo_consulta")
        valor_consulta = self._build_valor_consulta(
            tipo_consulta=tipo_consulta,
            validated=validated,
            value_field=value_field,
            extra_fields=extra_fields,
        )
        state.data["tipo_consulta"] = tipo_consulta
        state.data[value_field] = getattr(validated, value_field)
        if tipo_consulta == "auto_infracao":
            state.data["ano_auto_infracao"] = getattr(validated, "ano_auto_infracao")

        if self._consulta_cacheada_corresponde(
            state,
            tipo_consulta,
            valor_consulta,
        ):
            mensagem = (
                self._get_divida_ativa_context(state).get(
                    "mensagem_divida_contribuinte"
                )
                or DividaAtivaTemplates.consulta_realizada()
            )
            state.internal.pop(STATE_TIPO_CONSULTA_CACHE, None)
            return self._action_response(state, mensagem)

        dados = {}
        for payload_field, api_field in (extra_fields or {}).items():
            dados[api_field] = getattr(validated, payload_field)

        try:
            resultado = await self.api_service.consultar_debitos_por_no(
                node_name,
                valor=getattr(validated, value_field),
                **dados,
            )
        finally:
            state.internal.pop(STATE_TIPO_CONSULTA_CACHE, None)

        state.internal[STATE_CONSULTA_REALIZADA] = True

        if not resultado or not resultado.get("api_resposta_sucesso"):
            state.internal[STATE_TIPO_CONSULTA_CACHE] = tipo_consulta
            descricao_erro = (
                resultado.get("api_descricao_erro")
                if isinstance(resultado, dict)
                else None
            )
            return self._schema_response(
                state,
                (
                    f"{descricao_erro or DividaAtivaTemplates.consulta_sem_debitos()}\n\n"
                    f"{description}"
                ),
                schema,
            )

        mensagem = resultado.get("mensagem_divida_contribuinte") or (
            DividaAtivaTemplates.consulta_realizada()
        )
        self._set_divida_ativa_context(
            state,
            self._build_divida_ativa_data(
                tipo_consulta=tipo_consulta,
                valor_consulta=valor_consulta,
                divida_info=resultado,
                mensagem=mensagem,
            ),
        )

        return self._action_response(state, mensagem)

    @handle_errors
    async def _consultar_cpf_cnpj(self, state: ServiceState) -> ServiceState:
        return await self._consultar_por_identificador(
            state,
            node_name="consultar_cpf_cnpj",
            payload_model=CpfCnpjPayload,
            value_field="cpf_cnpj",
            description=DividaAtivaTemplates.consultar_cpf_cnpj(),
        )

    @handle_errors
    async def _consultar_inscricao_imobiliaria(
        self, state: ServiceState
    ) -> ServiceState:
        return await self._consultar_por_identificador(
            state,
            node_name="consultar_inscricao_imobiliaria",
            payload_model=InscricaoImobiliariaPayload,
            value_field="inscricao_imobiliaria",
            description=DividaAtivaTemplates.consultar_inscricao_imobiliaria(),
        )

    @handle_errors
    async def _consultar_auto_infracao(self, state: ServiceState) -> ServiceState:
        return await self._consultar_por_identificador(
            state,
            node_name="consultar_auto_infracao",
            payload_model=AutoInfracaoPayload,
            value_field="numero_auto_infracao",
            extra_fields={"ano_auto_infracao": "ano"},
            description=DividaAtivaTemplates.consultar_auto_infracao(),
        )

    @handle_errors
    async def _consultar_cda(self, state: ServiceState) -> ServiceState:
        return await self._consultar_por_identificador(
            state,
            node_name="consultar_cda",
            payload_model=CdaPayload,
            value_field="cda",
            description=DividaAtivaTemplates.consultar_cda(),
        )

    @handle_errors
    async def _consultar_execucao_fiscal(self, state: ServiceState) -> ServiceState:
        return await self._consultar_por_identificador(
            state,
            node_name="consultar_execucao_fiscal",
            payload_model=ExecucaoFiscalPayload,
            value_field="execucao_fiscal",
            description=DividaAtivaTemplates.consultar_execucao_fiscal(),
        )

    def _route_tipo_consulta(self, state: ServiceState) -> str:
        if state.agent_response is not None:
            return END

        tipo_consulta = self._tipo_consulta_atual(state)
        return self.tipo_consulta_steps.get(tipo_consulta, END)

    def build_graph(self) -> StateGraph:
        """Constrói o grafo LangGraph do workflow."""
        graph = StateGraph(ServiceState)

        graph.add_node("selecionar_tipo_consulta", self._selecionar_tipo_consulta)
        graph.add_node("consultar_cpf_cnpj", self._consultar_cpf_cnpj)
        graph.add_node(
            "consultar_inscricao_imobiliaria", self._consultar_inscricao_imobiliaria
        )
        graph.add_node("consultar_auto_infracao", self._consultar_auto_infracao)
        graph.add_node("consultar_cda", self._consultar_cda)
        graph.add_node("consultar_execucao_fiscal", self._consultar_execucao_fiscal)

        graph.set_entry_point("selecionar_tipo_consulta")

        graph.add_conditional_edges(
            "selecionar_tipo_consulta",
            self._route_tipo_consulta,
            {
                "consultar_cpf_cnpj": "consultar_cpf_cnpj",
                "consultar_inscricao_imobiliaria": "consultar_inscricao_imobiliaria",
                "consultar_auto_infracao": "consultar_auto_infracao",
                "consultar_cda": "consultar_cda",
                "consultar_execucao_fiscal": "consultar_execucao_fiscal",
                END: END,
            },
        )
        graph.add_edge("consultar_cpf_cnpj", END)
        graph.add_edge("consultar_inscricao_imobiliaria", END)
        graph.add_edge("consultar_auto_infracao", END)
        graph.add_edge("consultar_cda", END)
        graph.add_edge("consultar_execucao_fiscal", END)

        return graph
