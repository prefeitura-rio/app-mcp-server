import asyncio
import json
from typing import List, Optional
from langgraph.graph import StateGraph, END
from src.config.env import EQUIPMENTS_VALID_THEMES
from src.tools.multi_step_service.core.base_workflow import BaseWorkflow, handle_errors
from src.tools.multi_step_service.core.models import ServiceState, AgentResponse
from src.tools.multi_step_service.workflows.equipments.models import (
    EquipmentsSearchPayload,
    EquipmentsInstructionsPayload,
)
from src.tools.equipments_tools import (
    get_equipments_with_instructions,
    get_equipments_instructions,
    get_equipments_categories,
)


class EquipmentsWorkflow(BaseWorkflow):
    """
    Workflow para localização de equipamentos por endereço.

    Este workflow substitui a chamada direta das tools 'equipments_instructions' e 'equipments_by_address',
    unificando o fluxo de obtenção de instruções e busca.
    """

    service_name = "equipments_search"
    theme_payload = '{"tema": "valid_theme"}'
    description = f"Localização de equipamentos públicos por endereço. A primeira chamada deve ser obrigatoriamente {theme_payload}, temas validos ({EQUIPMENTS_VALID_THEMES}). Esse servico pode sofrer alteracoes constantes, entao seu uso é obrigatorio memos que você ja tenha informacoes no contexto."

    @handle_errors
    async def _get_instructions(self, state: ServiceState) -> ServiceState:
        """
        Passo 1: Obtém instruções e categorias baseadas no tema (se fornecido).
        Se o payload já contiver endereço (passo 2), apenas repassa para o próximo nó.
        """

        # 1. Check if it is a Search Request (Pass-through to Step 2)
        if "address" in state.payload or "address" in state.data:
            # If we have address, we assume the intention is to search.
            # We don't process it here, we let the conditional edge route to search.
            state.agent_response = None
            return state

        # 2. Process Instructions Request (Step 1)
        current_theme = "geral"

        if state.payload and "tema" in state.payload:
            try:
                validated_data = EquipmentsInstructionsPayload.model_validate(
                    state.payload
                )
                current_theme = validated_data.tema
            except Exception as e:
                state.agent_response = AgentResponse(
                    description=f"Tema inválido. Temas aceitos: {', '.join(EQUIPMENTS_VALID_THEMES)}",
                    payload_schema=EquipmentsInstructionsPayload.model_json_schema(),
                    error_message=f"Tema inválido: {str(e)}",
                )
                return state

        # Fetch instructions based on theme
        instructions_list = await get_equipments_instructions(tema=current_theme)
        categories_dict = await get_equipments_categories()

        # Clean up state data to save tokens
        state.data.pop("instrucoes_uso", None)
        state.data.pop("categorias_disponiveis", None)
        state.data.pop("aviso_importante", None)

        # Format categories
        categories_str = ""
        for secret, cats in categories_dict.items():
            categories_str += f"{secret}: {', '.join(cats)}\n"

        # Format instructions
        instructions_str = ""
        if isinstance(instructions_list, list):
            for item in instructions_list:
                if isinstance(item, dict):
                    instrucao = item.get("instrucao") or item.get("texto") or str(item)
                    instructions_str += f"- {instrucao}\n"
        else:
            instructions_str = str(instructions_list)

        # Optimized description for the Agent
        description_text = (
            f"INSTRUÇÕES ({current_theme}) E CATEGORIAS:\n\n"
            f"{instructions_str}\n"
            "CATEGORIAS DISPONÍVEIS:\n"
            f"{categories_str}\n\n"
            "AÇÃO NECESSÁRIA:\n"
            "1. Solicite o ENDEREÇO COMPLETO ao usuário.\n"
            "2. Na próxima chamada, envie 'address' e 'categories' (inferidas da lista acima com base no pedido do usuário)."
        )

        # Return to Agent asking for address, providing the Search Schema for the next step
        state.agent_response = AgentResponse(
            description=description_text,
            payload_schema=EquipmentsSearchPayload.model_json_schema(),
        )
        return state

    @handle_errors
    async def _search_equipments(self, state: ServiceState) -> ServiceState:
        """
        Passo 2: Realiza a busca dos equipamentos e gera as instruções finais.
        Recebe o payload com endereço e categorias.
        """

        # Process Payload if present (this node is the handler for Search Payload)
        if "address" in state.payload:
            try:
                validated_data = EquipmentsSearchPayload.model_validate(state.payload)
                state.data["address"] = validated_data.address
                state.data["categories"] = validated_data.categories
            except Exception as e:
                state.agent_response = AgentResponse(
                    description="Erro nos dados de busca.",
                    payload_schema=EquipmentsSearchPayload.model_json_schema(),
                    error_message=f"Dados de busca inválidos: {str(e)}",
                )
                return state

        address = state.data.get("address")
        categories = state.data.get("categories", [])

        if not address:
            # Should not happen if routing is correct, but safety check
            state.agent_response = AgentResponse(
                description="Endereço não fornecido. Por favor, reinicie o processo informando o endereço.",
                error_message="Endereço ausente no estado.",
            )
            return state

        # Call existing function
        result = await get_equipments_with_instructions(
            address=address, categories=categories
        )

        if "error" in result:
            error_data = result["error"]
            error_msg = "Erro ao buscar equipamentos."
            if isinstance(error_data, list) and len(error_data) > 0:
                error_msg = error_data[0].get("message", error_msg)
            elif isinstance(error_data, dict):
                error_msg = error_data.get("message", error_msg)

            state.agent_response = AgentResponse(
                description=f"Não foi possível localizar equipamentos: {error_msg}",
                payload_schema=EquipmentsSearchPayload.model_json_schema(),
                error_message=error_msg,
            )
            return state

        # Success
        final_instructions = result.get("instructions", "")
        state.data["equipamentos"] = result.get("equipamentos", [])

        # The description contains formatted instructions for the agent to reply to the user
        state.agent_response = AgentResponse(
            description=final_instructions,
            payload_schema=None,  # End of flow
        )

        return state

    def _check_status(self, state: ServiceState) -> str:
        """
        Roteador Principal.
        Decide se vai para 'search_equipments' (se tiver endereço) ou para END (se estiver pedindo input).
        """
        # Se tem endereço (no payload atual ou salvo), vamos para a busca
        if "address" in state.payload or "address" in state.data:
            return "search"

        # Caso contrário, se já geramos uma resposta (instruções), paramos para o usuário responder
        if state.agent_response is not None:
            return END

        # Fallback (não deveria acontecer no fluxo normal)
        return END

    def build_graph(self) -> StateGraph[ServiceState]:
        graph = StateGraph(ServiceState)

        # Renamed nodes for clarity
        graph.add_node("get_instructions", self._get_instructions)
        graph.add_node("search", self._search_equipments)

        graph.set_entry_point("get_instructions")

        graph.add_conditional_edges(
            "get_instructions", self._check_status, {"search": "search", END: END}
        )

        graph.add_edge("search", END)

        return graph
