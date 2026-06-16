from langgraph.graph import END, StateGraph
from loguru import logger

from src.tools.multi_step_service.core import (
    AgentResponse,
    BaseWorkflow,
    ServiceState,
    handle_errors,
)
from src.tools.multi_step_service.workflows.divida_ativa.api_service import (
    DividaAtivaAPIService,
)
from src.tools.multi_step_service.workflows.divida_ativa.models import (
    AcaoDebitosPayload,
    AnoAutoInfracaoPayload,
    ConfirmacaoPayload,
    ItensPagamentoPayload,
    TipoConsultaPayload,
    ValorConsultaPayload,
)
from src.tools.multi_step_service.workflows.divida_ativa.templates import (
    DividaAtivaTemplates,
)


class DividaAtivaWorkflow(BaseWorkflow):
    service_name = "divida_ativa"
    description = "Consulta de débitos de Dívida Ativa e emissão de guias."
    automatic_resets = True
    step_order = [
        "consulta_debitos",
        "anoAutoInfracao",
        "valor_consulta",
        "acao",
        "itens_informados",
        "confirmacao_debitos",
    ]
    step_dependencies = {
        "consulta_debitos": [
            "anoAutoInfracao",
            "valor_consulta",
            "consulta_resultado",
            "acao",
            "itens_informados",
            "confirmacao_debitos",
            "guia_emitida",
        ],
        "anoAutoInfracao": [
            "valor_consulta",
            "consulta_resultado",
            "acao",
            "itens_informados",
            "confirmacao_debitos",
            "guia_emitida",
        ],
        "valor_consulta": [
            "consulta_resultado",
            "acao",
            "itens_informados",
            "confirmacao_debitos",
            "guia_emitida",
        ],
        "acao": ["itens_informados", "confirmacao_debitos", "guia_emitida"],
        "itens_informados": ["confirmacao_debitos", "guia_emitida"],
        "confirmacao_debitos": ["guia_emitida"],
    }

    def __init__(self):
        super().__init__()
        self.api_service = DividaAtivaAPIService()

    @handle_errors
    async def _escolher_tipo_consulta(self, state: ServiceState) -> ServiceState:
        # Se veio do WhatsApp Flow, normaliza o payload e marca flow como preenchido
        if state.payload.get("_source") == "whatsapp_flow":
            from src.tools.divida_ativa_flow import normalize_flow_submission

            normalized = normalize_flow_submission(state.payload)
            state.payload.update(normalized)
            state.data["_flow_completed"] = True

        if "consulta_debitos" in state.payload:
            payload = TipoConsultaPayload.model_validate(state.payload)
            state.data["consulta_debitos"] = payload.consulta_debitos
            state.agent_response = None
            return state

        if "consulta_debitos" in state.data:
            state.agent_response = None
            return state

        state.agent_response = AgentResponse(
            description=DividaAtivaTemplates.escolher_tipo_consulta(),
            payload_schema=TipoConsultaPayload.model_json_schema(),
        )
        return state

    @handle_errors
    async def _coletar_ano_auto_infracao(self, state: ServiceState) -> ServiceState:
        if state.data.get("consulta_debitos") != "numeroAutoInfracao":
            state.agent_response = None
            return state

        if "anoAutoInfracao" in state.payload:
            payload = AnoAutoInfracaoPayload.model_validate(state.payload)
            state.data["anoAutoInfracao"] = payload.anoAutoInfracao
            state.agent_response = None
            return state

        if "anoAutoInfracao" in state.data:
            state.agent_response = None
            return state

        state.agent_response = AgentResponse(
            description=DividaAtivaTemplates.solicitar_ano_auto(),
            payload_schema=AnoAutoInfracaoPayload.model_json_schema(),
        )
        return state

    @handle_errors
    async def _coletar_valor_consulta(self, state: ServiceState) -> ServiceState:
        tipo = state.data.get("consulta_debitos")
        if tipo in state.payload:
            payload = ValorConsultaPayload.model_validate(state.payload)
            valor = getattr(payload, tipo)
            state.data["valor_consulta"] = valor
            state.data[tipo] = valor
            state.agent_response = None
            return state

        if "valor_consulta" in state.data:
            state.agent_response = None
            return state

        state.agent_response = AgentResponse(
            description=DividaAtivaTemplates.solicitar_valor(tipo),
            payload_schema=ValorConsultaPayload.model_json_schema(),
        )
        return state

    @handle_errors
    async def _consultar_debitos(self, state: ServiceState) -> ServiceState:
        if "consulta_resultado" in state.data:
            state.agent_response = None
            return state

        tipo = state.data["consulta_debitos"]
        valor = state.data["valor_consulta"]
        ano = state.data.get("anoAutoInfracao")
        logger.info(f"Consultando dívida ativa por {tipo}: {valor}")
        resultado = await self.api_service.consultar_debitos(tipo, valor, ano)

        if not resultado.get("api_resposta_sucesso"):
            state.agent_response = AgentResponse(
                description=resultado.get("api_descricao_erro", "Erro na consulta."),
                error_message=resultado.get("api_descricao_erro"),
                payload_schema=ValorConsultaPayload.model_json_schema(),
            )
            return state

        state.data["consulta_resultado"] = resultado
        state.agent_response = None
        return state

    @handle_errors
    async def _escolher_acao(self, state: ServiceState) -> ServiceState:
        consulta = state.data["consulta_resultado"]
        mensagem_consulta = consulta.get("mensagem_divida_contribuinte", "")
        total_nao_parcelado = consulta.get("total_nao_parcelado", 0)
        total_parcelado = consulta.get("total_parcelado", 0)

        if total_nao_parcelado == 0 and total_parcelado == 0:
            state.agent_response = AgentResponse(
                service_name=self.service_name,
                description=DividaAtivaTemplates.nenhuma_divida(mensagem_consulta),
                data=state.data,
            )
            state.data["_reset_on_next_call"] = True
            return state

        if "acao" in state.payload:
            payload = AcaoDebitosPayload.model_validate(state.payload)
            if not self._acao_disponivel(
                payload.acao, total_nao_parcelado, total_parcelado
            ):
                state.agent_response = AgentResponse(
                    description=DividaAtivaTemplates.opcao_invalida()
                    + "\n\n"
                    + DividaAtivaTemplates.escolher_acao(
                        total_nao_parcelado, total_parcelado
                    ),
                    payload_schema=AcaoDebitosPayload.model_json_schema(),
                )
                return state
            state.data["acao"] = payload.acao
            state.agent_response = None
            return state

        if "acao" in state.data:
            state.agent_response = None
            return state

        state.agent_response = AgentResponse(
            description=mensagem_consulta
            + "\n\n"
            + DividaAtivaTemplates.escolher_acao(total_nao_parcelado, total_parcelado),
            payload_schema=AcaoDebitosPayload.model_json_schema(),
            data={"consulta_resultado": consulta},
        )
        return state

    @handle_errors
    async def _resolver_acao_informativa(self, state: ServiceState) -> ServiceState:
        acao = state.data.get("acao")
        if acao == "parcelar_debitos":
            state.agent_response = AgentResponse(
                service_name=self.service_name,
                description=DividaAtivaTemplates.link_parcelamento(),
                data=state.data,
            )
            state.data["_reset_on_next_call"] = True
            return state
        if acao == "liquidar_parcelamento":
            state.agent_response = AgentResponse(
                service_name=self.service_name,
                description=DividaAtivaTemplates.link_liquidacao(),
                data=state.data,
            )
            state.data["_reset_on_next_call"] = True
            return state
        if acao == "emitir_segunda_via":
            state.agent_response = AgentResponse(
                service_name=self.service_name,
                description=DividaAtivaTemplates.link_segunda_via(),
                data=state.data,
            )
            state.data["_reset_on_next_call"] = True
            return state

        state.agent_response = None
        return state

    @handle_errors
    async def _coletar_itens(self, state: ServiceState) -> ServiceState:
        consulta = state.data["consulta_resultado"]
        acao = state.data["acao"]
        total = (
            consulta.get("total_nao_parcelado", 0)
            if acao == "pagar_a_vista"
            else consulta.get("total_parcelado", 0)
        )

        if total == 1:
            state.data["itens_informados"] = [self._unico_item_para_acao(state)]
            state.agent_response = None
            return state

        if (
            "itens_informados" in state.payload
            or "todos_itens_informados" in state.payload
        ):
            payload = ItensPagamentoPayload.model_validate(state.payload)

            # Se todos_itens_informados=True OU itens_informados=None (sem números extraídos)
            # interpretamos como "quero todos"
            if payload.todos_itens_informados or payload.itens_informados is None:
                state.data["itens_informados"] = self._todos_itens_para_acao(state)
                state.agent_response = None
                return state

            # Caso contrário, usa os itens específicos informados
            state.data["itens_informados"] = payload.itens_informados

            if not self._itens_validos_para_acao(state, state.data["itens_informados"]):
                state.data.pop("itens_informados", None)
                state.agent_response = AgentResponse(
                    description=DividaAtivaTemplates.opcao_invalida()
                    + "\n\n"
                    + DividaAtivaTemplates.solicitar_itens(acao),
                    payload_schema=ItensPagamentoPayload.model_json_schema(),
                )
                return state

            state.agent_response = None
            return state

        if "itens_informados" in state.data:
            state.agent_response = None
            return state

        state.agent_response = AgentResponse(
            description=DividaAtivaTemplates.solicitar_itens(acao),
            payload_schema=ItensPagamentoPayload.model_json_schema(),
        )
        return state

    @handle_errors
    async def _confirmar_debitos(self, state: ServiceState) -> ServiceState:
        """Mostra débitos selecionados e pede confirmação antes de emitir guia."""
        if "confirmacao_debitos" in state.data:
            state.agent_response = None
            return state

        # Pega os débitos selecionados
        consulta = state.data["consulta_resultado"]
        itens_selecionados = state.data["itens_informados"]
        acao = state.data["acao"]

        # Constrói a lista de débitos com detalhes
        debitos_detalhados = []
        dict_itens = consulta.get("dicionario_itens", {})
        debitos_msg = consulta.get("debitos_msg", [])

        for idx in itens_selecionados:
            # Encontra o débito correspondente na lista original
            for debito in debitos_msg:
                valor_debito = dict_itens.get(idx) or dict_itens.get(str(idx))
                if (
                    ("cda" in debito and str(debito["cda"]) == str(valor_debito))
                    or ("ef" in debito and str(debito["ef"]) == str(valor_debito))
                    or ("guia" in debito and str(debito["guia"]) == str(valor_debito))
                ):
                    debitos_detalhados.append(debito)
                    break

        # Pede confirmação
        if "confirma" in state.payload:
            payload = ConfirmacaoPayload.model_validate(state.payload)

            if not payload.confirma:
                # Usuário cancelou - volta para seleção de itens
                state.data.pop("itens_informados", None)
                state.agent_response = AgentResponse(
                    description="Entendido. Vamos selecionar os débitos novamente.\n\n"
                    + DividaAtivaTemplates.solicitar_itens(acao),
                    payload_schema=ItensPagamentoPayload.model_json_schema(),
                )
                return state

            # Usuário confirmou
            state.data["confirmacao_debitos"] = True
            state.agent_response = None
            return state

        # Mostra débitos e pede confirmação
        state.agent_response = AgentResponse(
            description=DividaAtivaTemplates.confirmar_debitos_selecionados(
                debitos_detalhados, acao
            ),
            payload_schema=ConfirmacaoPayload.model_json_schema(),
        )
        return state

    @handle_errors
    async def _emitir_guia(self, state: ServiceState) -> ServiceState:
        if "guia_emitida" in state.data:
            return state

        tipo_emissao = (
            "a_vista" if state.data["acao"] == "pagar_a_vista" else "regularizacao"
        )
        resultado = await self.api_service.emitir_guia(
            state.data["consulta_resultado"],
            state.data["itens_informados"],
            tipo_emissao,
        )

        if resultado.get("opcao_invalida"):
            state.data.pop("itens_informados", None)
            state.agent_response = AgentResponse(
                description=DividaAtivaTemplates.opcao_invalida(),
                payload_schema=ItensPagamentoPayload.model_json_schema(),
            )
            return state

        if not resultado.get("api_resposta_sucesso"):
            state.agent_response = AgentResponse(
                description=(
                    "Encontramos o erro, que é o seguinte:\n\n"
                    f"{resultado.get('api_descricao_erro', 'Erro ao emitir guia.')}"
                ),
                error_message=resultado.get("api_descricao_erro"),
            )
            state.data["_reset_on_next_call"] = True
            return state

        state.data["guia_emitida"] = resultado
        state.agent_response = AgentResponse(
            service_name=self.service_name,
            description=DividaAtivaTemplates.guia_emitida(resultado),
            data={"guia_emitida": resultado},
        )
        state.data["_reset_on_next_call"] = True
        return state

    def _acao_disponivel(
        self, acao: str, total_nao_parcelado: int, total_parcelado: int
    ) -> bool:
        if acao in {"pagar_a_vista", "parcelar_debitos"}:
            return total_nao_parcelado > 0
        return total_parcelado > 0

    def _indices_por_acao(self, state: ServiceState) -> list[int]:
        consulta = state.data["consulta_resultado"]
        dict_itens = consulta.get("dicionario_itens", {}) or {}
        if state.data.get("acao") == "pagar_a_vista":
            validos = set(consulta.get("lista_cdas", []) or []) | set(
                consulta.get("lista_efs", []) or []
            )
        else:
            validos = set(consulta.get("lista_guias", []) or [])
        return [int(seq) for seq, valor in dict_itens.items() if str(valor) in validos]

    def _todos_itens_para_acao(self, state: ServiceState) -> list[int]:
        return self._indices_por_acao(state)

    def _unico_item_para_acao(self, state: ServiceState) -> int:
        indices = self._indices_por_acao(state)
        return indices[0]

    def _itens_validos_para_acao(self, state: ServiceState, itens: list[int]) -> bool:
        validos = set(self._indices_por_acao(state))
        return bool(itens) and all(item in validos for item in itens)

    def _decide_after_node(self, state: ServiceState):
        if state.agent_response is not None:
            return END
        return "continue"

    def _route_after_action_info(self, state: ServiceState):
        if state.agent_response is not None:
            return END
        return "continue"

    def build_graph(self) -> StateGraph[ServiceState]:
        graph = StateGraph(ServiceState)
        graph.add_node("escolher_tipo_consulta", self._escolher_tipo_consulta)
        graph.add_node("coletar_ano_auto_infracao", self._coletar_ano_auto_infracao)
        graph.add_node("coletar_valor_consulta", self._coletar_valor_consulta)
        graph.add_node("consultar_debitos", self._consultar_debitos)
        graph.add_node("escolher_acao", self._escolher_acao)
        graph.add_node("resolver_acao_informativa", self._resolver_acao_informativa)
        graph.add_node("coletar_itens", self._coletar_itens)
        graph.add_node("confirmar_debitos", self._confirmar_debitos)
        graph.add_node("emitir_guia", self._emitir_guia)

        graph.set_entry_point("escolher_tipo_consulta")
        graph.add_conditional_edges(
            "escolher_tipo_consulta",
            self._decide_after_node,
            {"continue": "coletar_ano_auto_infracao", END: END},
        )
        graph.add_conditional_edges(
            "coletar_ano_auto_infracao",
            self._decide_after_node,
            {"continue": "coletar_valor_consulta", END: END},
        )
        graph.add_conditional_edges(
            "coletar_valor_consulta",
            self._decide_after_node,
            {"continue": "consultar_debitos", END: END},
        )
        graph.add_conditional_edges(
            "consultar_debitos",
            self._decide_after_node,
            {"continue": "escolher_acao", END: END},
        )
        graph.add_conditional_edges(
            "escolher_acao",
            self._decide_after_node,
            {"continue": "resolver_acao_informativa", END: END},
        )
        graph.add_conditional_edges(
            "resolver_acao_informativa",
            self._route_after_action_info,
            {"continue": "coletar_itens", END: END},
        )
        graph.add_conditional_edges(
            "coletar_itens",
            self._decide_after_node,
            {"continue": "confirmar_debitos", END: END},
        )
        graph.add_conditional_edges(
            "confirmar_debitos",
            self._decide_after_node,
            {"continue": "emitir_guia", END: END},
        )
        graph.add_edge("emitir_guia", END)
        return graph
