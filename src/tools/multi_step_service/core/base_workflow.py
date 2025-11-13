from abc import ABC, abstractmethod
from typing import Dict, Any, Callable
import os
from functools import wraps
import traceback
import inspect

from langgraph.graph import StateGraph

from src.tools.multi_step_service.core.models import ServiceState, AgentResponse

from loguru import logger


class BaseWorkflow(ABC):
    """
    Classe base para todos os workflows V5.

    Cada workflow deve herdar desta classe e implementar:
    - service_name: Nome do serviço
    - description: Descrição do serviço
    - build_graph(): Método que constrói o StateGraph.
    """

    service_name: str = ""
    description: str = ""

    @abstractmethod
    def build_graph(self) -> StateGraph[ServiceState]:
        """
        Constrói e retorna o grafo LangGraph para este workflow.
        Este método deve ser implementado por cada workflow filho.
        """
        pass

    async def execute(
        self, state: ServiceState, payload: Dict[str, Any]
    ) -> ServiceState:
        """
        Executa o workflow de forma assíncrona com o estado e payload fornecidos.

        Este método orquestra a execução do grafo LangGraph:
        1. Injeta payload no state (fonte única da verdade)
        2. Compila o grafo.
        3. Invoca o grafo de forma assíncrona, executando em cascata até pausar ou terminar.
        4. Retorna o ServiceState atualizado.

        Benefícios da versão async:
        - Elimina overhead de múltiplos asyncio.run()
        - Permite paralelização de operações I/O nos nós
        - Nós do workflow podem usar await diretamente
        """

        # 1. Injeta payload no state - fonte única da verdade
        state.payload = payload or {}

        # 2. Compila o grafo definido no workflow específico
        graph = self.build_graph()
        compiled_graph = graph.compile()

        # 3. Invoca o grafo de forma assíncrona
        final_state_result = await compiled_graph.ainvoke(state)

        # O LangGraph pode retornar o ServiceState diretamente ou como dict
        # Vamos garantir que sempre trabalhamos com ServiceState
        if isinstance(final_state_result, ServiceState):
            final_state = final_state_result
        else:
            # Se retornar dict, convertemos de volta para ServiceState preservando campos obrigatórios
            if "user_id" not in final_state_result:
                final_state_result["user_id"] = state.user_id
            if "service_name" not in final_state_result:
                final_state_result["service_name"] = state.service_name

            final_state = ServiceState(**final_state_result)

        # Se o grafo terminou sem uma resposta explícita, significa que o serviço foi concluído.
        if final_state.agent_response is None:
            final_state.status = "completed"
            final_state.agent_response = AgentResponse(
                service_name=self.service_name,
                description="Serviço concluído com sucesso.",
                data=final_state.data,
            )

        # Limpa o payload para não persistir (dados temporários)
        temp_agent_response = final_state.agent_response
        final_state.payload = {}

        # Mantém a resposta para o orchestrator
        final_state.agent_response = AgentResponse(
            service_name=self.service_name,
            error_message=temp_agent_response.error_message,
            description=temp_agent_response.description,
            payload_schema=temp_agent_response.payload_schema,
            data=final_state.data,
        )

        return final_state

    def save_graph_image(self) -> str:
        """
        Salva a imagem do grafo compilado na mesma pasta do workflow.

        Returns:
            Caminho para o arquivo de imagem salvo
        """
        try:
            # Constrói e compila o grafo
            graph = self.build_graph()
            compiled_graph = graph.compile()

            # Determina o diretório do arquivo do workflow
            workflow_file = self.__class__.__module__.replace(".", "/") + ".py"
            workflow_dir = os.path.dirname(workflow_file)

            # Se não conseguir determinar o diretório, usa o diretório atual
            if not workflow_dir or not os.path.exists(workflow_dir):
                workflow_dir = os.path.dirname(os.path.abspath(__file__))
                workflow_dir = os.path.join(workflow_dir, "..", "workflows")

            # Cria o caminho completo para a imagem
            image_filename = f"{self.service_name}.png"
            image_path = os.path.join(workflow_dir, image_filename)

            # diagram = compiled_graph.get_graph().draw_mermaid()
            g = compiled_graph.get_graph()
            logger.info(f"\n{g.draw_mermaid()}")
            # Gera e salva a imagem do grafo
            # with open(image_path, "wb") as f:
            #     f.write(g.draw_mermaid_png())

            return image_path

        except Exception as e:
            raise


def handle_errors(node_func: Callable) -> Callable:
    """
    Decorator para envolver nós assíncronos do grafo com tratamento de exceções.
    Preserva a AgentResponse preparada pelo nó mesmo em caso de erro.

    Nota: Atualmente suporta apenas funções assíncronas (async def).
    """

    if not inspect.iscoroutinefunction(node_func):
        raise TypeError(
            f"handle_errors decorator requer função assíncrona. "
            f"'{node_func.__name__}' não é async def."
        )

    @wraps(node_func)
    async def wrapper(instance, state: ServiceState) -> ServiceState:
        try:
            return await node_func(instance, state)
        except Exception as e:
            traceback_str = traceback.format_exc()
            logger.error(
                f"\nError in service: {state.service_name}\nuser_id:{state.user_id}\nnode:{node_func.__name__}\n{traceback_str}",
            )
            # Pega a AgentResponse que o nó já deve ter colocado no estado.
            # Se, por algum motivo, não houver uma, cria uma nova.
            response = state.agent_response or AgentResponse()

            # Adiciona a mensagem de erro da exceção à resposta existente.
            # A descrição e o schema que já estavam lá são preservados.
            response.error_message = str(e)

            state.agent_response = response
            state.status = "error"

            return state

    return wrapper
