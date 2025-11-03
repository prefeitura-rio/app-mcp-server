import random
from typing import Literal, Optional
from pydantic import BaseModel, Field

from langgraph.graph import StateGraph, END

from src.tools.multi_step_service.core.base_workflow import BaseWorkflow, handle_errors
from src.tools.multi_step_service.core.models import ServiceState, AgentResponse


class UserInfoData(BaseModel):
    name: str = Field(..., min_length=2)
    email: str = Field(..., pattern=r"^[^@]+@[^@]+\.[^@]+$")


class UserInfoPayload(BaseModel):
    # Suporte para dados nested
    user_info: Optional[UserInfoData] = None


class AccountTypePayload(BaseModel):
    account_type: Literal["checking", "savings"]


class ActionChoicePayload(BaseModel):
    ask_action: Literal["deposit", "balance"]


class DepositAmountPayload(BaseModel):
    deposit_amount: float = Field(..., gt=0)


class BankAccountWorkflow(BaseWorkflow):
    service_name = "bank_account"
    description = (
        "Abre uma conta bancária e permite operações como depósito e consulta de saldo."
    )

    # --- Nós do Grafo ---
    @handle_errors
    async def _collect_user_info(self, state: ServiceState) -> ServiceState:
        if "user_info" in state.data:
            return state

        response = AgentResponse(
            description="Colete as informações do usuário.",
            payload_schema=UserInfoPayload.model_json_schema(),
        )
        state.agent_response = response
        if "user_info" in state.payload:
            validated_data = UserInfoPayload.model_validate(state.payload)
            if validated_data.user_info:
                state.data["user_info"] = validated_data.user_info.model_dump()
                state.agent_response = None
        state.payload.pop("user_info", None)  # Consumir user_info
        return state

    @handle_errors
    async def _collect_account_type(self, state: ServiceState) -> ServiceState:
        if "account_type" in state.data:
            return state

        response = AgentResponse(
            description="Qual tipo de conta você gostaria de abrir: 'checking' (corrente) ou 'savings' (poupança)?",
            payload_schema=AccountTypePayload.model_json_schema(),
        )
        state.agent_response = response
        if "account_type" in state.payload:
            validated_data = AccountTypePayload.model_validate(state.payload)
            state.data.update(validated_data.model_dump())
            state.agent_response = None

        state.payload.pop("account_type", None)
        return state

    def _create_account(self, state: ServiceState) -> ServiceState:
        state.data["account_number"] = random.randint(10000, 99999)
        state.data["balance"] = 0.0
        return state

    @handle_errors
    async def _ask_action(self, state: ServiceState) -> ServiceState:
        # Só ativar ask_action quando pending_action é None
        if state.internal.get("pending_action") is not None:
            return state

        # Sempre pedir ação (não persistir ask_action)
        response = AgentResponse(
            description="O que você gostaria de fazer? 'deposit' (depositar) ou 'balance' (ver saldo)?",
            payload_schema=ActionChoicePayload.model_json_schema(),
        )
        state.agent_response = response

        # Se veio ação no payload, armazenar no internal para persistir através dos steps
        if "ask_action" in state.payload:
            ActionChoicePayload.model_validate(state.payload)  # Validar
            # Armazenar no internal para não perder a informação
            state.internal["pending_action"] = state.payload["ask_action"]
            state.agent_response = None  # Continuar fluxo
        return state

    @handle_errors
    async def _get_balance(self, state: ServiceState) -> ServiceState:
        # Exibir saldo e limpar pending_action
        balance = state.data.get("balance", 0.0)
        state.agent_response = AgentResponse(
            description=f"💰 Saldo atual da conta R$ {balance:.2f}.",
            payload_schema=ActionChoicePayload.model_json_schema(),
        )

        # Limpar pending_action após mostrar o saldo
        state.internal.pop("pending_action", None)
        return state

    @handle_errors
    async def _collect_deposit_amount(self, state: ServiceState) -> ServiceState:
        if "deposit_amount" in state.data:
            return state

        response = AgentResponse(
            description="Qual valor você gostaria de depositar?",
            payload_schema=DepositAmountPayload.model_json_schema(),
        )
        state.agent_response = response
        if "deposit_amount" in state.payload:
            validated_data = DepositAmountPayload.model_validate(state.payload)
            state.data.update(validated_data.model_dump())
            state.agent_response = None
        return state

    @handle_errors
    async def _make_deposit(self, state: ServiceState) -> ServiceState:
        amount = state.data.get("deposit_amount", 0)
        current_balance = state.data.get("balance", 0)
        new_balance = current_balance + amount
        state.data["balance"] = new_balance

        # Limpar deposit_amount e pending_action após usar
        state.data.pop("deposit_amount", None)
        state.internal.pop("pending_action", None)

        # Confirma o depósito realizado
        state.agent_response = AgentResponse(
            description=f"✅ Depósito de R$ {amount:.2f} realizado com sucesso! Novo saldo: R$ {new_balance:.2f}",
            payload_schema=ActionChoicePayload.model_json_schema(),
        )
        return state

    # --- Roteadores Condicionais (Lógica de roteamento e pausa) ---

    def _decide_after_data_collection(self, state: ServiceState):
        # Roteador genérico para nós de coleta de dados.
        # Se o nó pediu input, a execução para. Senão, continua.
        if state.agent_response is not None:
            return END
        return "continue"

    def _route_after_user_info(self, state: ServiceState) -> str:
        # Verifica se já existe account_number (conta já existe)
        if state.data.get("account_number"):
            return "ask_action"
        else:
            return "account_type"

    def _route_after_action_choice(self, state: ServiceState) -> str:
        # Roteador que decide próximo nó baseado na ação armazenada no internal
        action = state.internal.get("pending_action")
        if action == "deposit":
            return "collect_deposit_amount"
        elif action == "balance":
            return "get_balance"
        # Para qualquer outra ação ou sem ação, volta para ask_action
        return "ask_action"

    # --- Construção do Grafo ---

    def build_graph(self) -> StateGraph[ServiceState]:
        graph = StateGraph(ServiceState)

        graph.add_node("collect_user_info", self._collect_user_info)
        graph.add_node("account_type", self._collect_account_type)
        graph.add_node("create_account", self._create_account)
        graph.add_node("ask_action", self._ask_action)
        graph.add_node("get_balance", self._get_balance)
        graph.add_node("collect_deposit_amount", self._collect_deposit_amount)
        graph.add_node("make_deposit", self._make_deposit)

        graph.set_entry_point("collect_user_info")

        # Arestas após collect_user_info: se coletou dados, decide próximo step
        graph.add_conditional_edges(
            "collect_user_info",
            self._decide_after_data_collection,
            {"continue": "route_user_info", END: END},
        )

        # Roteamento após user_info: verifica se conta já existe
        graph.add_node("route_user_info", lambda state: state)
        graph.add_conditional_edges(
            "route_user_info",
            self._route_after_user_info,
            {"account_type": "account_type", "ask_action": "ask_action"},
        )

        # <-- MUDANÇA: Aresta condicional aqui também
        graph.add_conditional_edges(
            "account_type",
            self._decide_after_data_collection,
            {"continue": "create_account", END: END},
        )

        graph.add_edge("create_account", "ask_action")

        # <-- MUDANÇA: Roteamento após 'ask_action' agora é um processo de duas etapas
        # 1. Verifica se precisa pausar
        graph.add_conditional_edges(
            "ask_action",
            self._decide_after_data_collection,
            {
                # 2. Se não pausar, decide para onde ir com base na ação
                "continue": "route_action_choice",
                END: END,
            },
        )

        # Adicionamos um nó "invisível" que apenas roteia
        graph.add_node("route_action_choice", lambda state: state)
        graph.add_conditional_edges(
            "route_action_choice",
            self._route_after_action_choice,
            {
                "collect_deposit_amount": "collect_deposit_amount",
                "get_balance": "get_balance",
                "ask_action": "ask_action",
            },
        )

        # <-- MUDANÇA: Aresta condicional para a coleta de valor
        graph.add_conditional_edges(
            "collect_deposit_amount",
            self._decide_after_data_collection,
            {"continue": "make_deposit", END: END},
        )

        graph.add_conditional_edges(
            "make_deposit",
            self._decide_after_data_collection,
            {"continue": "ask_action", END: END},
        )

        graph.add_conditional_edges(
            "get_balance",
            self._decide_after_data_collection,
            {"continue": "ask_action", END: END},
        )

        return graph
