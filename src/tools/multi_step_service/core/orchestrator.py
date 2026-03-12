from typing import Dict, Type, Optional
from src.tools.multi_step_service.core.models import (
    ServiceRequest,
    ServiceState,
    AgentResponse,
)
from src.tools.multi_step_service.core.state import StateManager, StateMode
from src.tools.multi_step_service.workflows import workflows
from src.utils.error_interceptor import interceptor


class Orchestrator:
    """
    Orquestrador responsável por gerenciar workflows:
    - Listar workflows existentes
    - Executar workflows
    - Auto-inicializa com state manager e workflows

    Permite configurar modo de persistência (JSON, REDIS, BOTH).
    """

    def __init__(
        self,
        backend_mode: StateMode = StateMode.JSON,
        redis_url: Optional[str] = None,
        data_dir: str = "src/tools/multi_step_service/data",
    ):
        """
        Inicializa o Orchestrator.

        Args:
            backend_mode: Modo de persistência (padrão: StateMode.JSON)
            redis_url: URL Redis (opcional, usa REDIS_URL da env se None)
            data_dir: Diretório para arquivos JSON
        """
        self.workflows: Dict[str, Type] = {}
        self.backend_mode = backend_mode
        self.redis_url = redis_url
        self.data_dir = data_dir

        # Importa workflows automaticamente usando service_name
        for workflow_class in workflows:
            if hasattr(workflow_class, "service_name"):
                self.workflows[workflow_class.service_name] = workflow_class

    def list_workflows(self) -> Dict[str, str]:
        """
        Lista todos os workflows registrados.

        Returns:
            Dicionário com {service_name: description} dos workflows disponíveis
        """
        result = {}
        for service_name, workflow_class in self.workflows.items():
            # Pega description do workflow (atributo description ou __doc__)
            description = getattr(workflow_class, "description", None)
            if not description:
                description = getattr(
                    workflow_class, "__doc__", "Sem descrição"
                ).strip()
                description = (
                    description.split("\n")[0] if description else "Sem descrição"
                )

            result[service_name] = description

        return result

    def save_workflow_graph_image(self, service_name: str) -> str:
        """
        Salva a imagem do grafo para um workflow específico.

        Args:
            service_name: Nome do serviço/workflow

        Returns:
            Caminho para o arquivo de imagem salvo

        Raises:
            ValueError: Se workflow não for encontrado
        """

        # Verifica se workflow existe
        if service_name not in self.workflows:
            available = ", ".join(self.list_workflows())
            raise ValueError(
                f"Serviço '{service_name}' não encontrado. Disponíveis: {available}"
            )

        # Instancia o workflow e salva a imagem
        workflow_class = self.workflows[service_name]
        workflow = workflow_class()

        return workflow.save_graph_image()

    def save_all_workflow_graphs(self) -> Dict[str, str]:
        """
        Salva as imagens dos grafos para todos os workflows registrados.

        Returns:
            Dicionário com {service_name: image_path} dos arquivos salvos
        """

        results = {}
        for service_name in self.workflows.keys():
            try:
                image_path = self.save_workflow_graph_image(service_name)
                results[service_name] = image_path

            except Exception as e:
                results[service_name] = f"Erro: {str(e)}"

        return results

    @interceptor(
        source={"source": "mcp", "tool": "multi_step_service"},
        extract_user_id=lambda args, kwargs: (
            kwargs.get("request").user_id if kwargs.get("request")
            else (args[1].user_id if len(args) > 1 else "unknown")
        ),
        extract_source=lambda args, kwargs, base: {
            **base,
            "workflow": (kwargs.get("request") or args[1]).service_name if (kwargs.get("request") or len(args) > 1) else "unknown"
        },
    )
    async def execute_workflow(self, request: ServiceRequest) -> AgentResponse:
        """
        Executa um workflow com base na requisição do agente.

        Args:
            request: Requisição contendo service_name, user_id e payload

        Returns:
            Resposta formatada para o agente

        Raises:
            ValueError: Se workflow não for encontrado
        """
        # Verifica se workflow existe
        if (not request.service_name) or (request.service_name not in self.workflows):
            available = ", ".join(self.list_workflows())
            return AgentResponse(
                service_name=request.service_name,
                error_message=f"Serviço '{request.service_name}' não encontrado. **Serviços Disponíveis:**\n\n{available}",
                description="",
                payload_schema=None,
                data={},
            )

        # Cria StateManager específico para este user_id com configurações do Orchestrator
        state_manager = StateManager(
            user_id=request.user_id,
            backend_mode=self.backend_mode,
            redis_url=self.redis_url,
            data_dir=self.data_dir,
        )

        # Carrega ou cria state do serviço (async)
        state = await state_manager.load_service_state(request.service_name)

        if state is None:
            # Cria novo state se não existir
            state = ServiceState(
                user_id=request.user_id,
                service_name=request.service_name,
                status="progress",
                data={},
            )

        # Instancia e executa workflow
        workflow_class = self.workflows[request.service_name]
        workflow = workflow_class()

        try:
            # Executa workflow passando state e payload (async)
            # O workflow retorna ServiceState com agent_response integrado
            final_state = await workflow.execute(state, request.payload)

            # Salva state atualizado APÓS execução do workflow (async)
            # O state foi modificado durante a execução
            await state_manager.save_service_state(final_state)

            # Retorna a resposta do agente que está integrada no ServiceState
            return final_state.agent_response

        except Exception as e:
            # Em caso de erro, retorna resposta de erro
            return AgentResponse(
                service_name=request.service_name,
                error_message=f"Erro na execução do serviço: {str(e)}",
                description="Erro interno do serviço",
                payload_schema=None,
                data=state.data,
            )
