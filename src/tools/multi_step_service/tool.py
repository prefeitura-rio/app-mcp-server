from typing import Any, Dict, Optional
from langchain_core.tools import tool

from src.tools.multi_step_service.core import (
    Orchestrator,
    ServiceRequest,
    tools_description,
)


@tool(description=tools_description)
async def multi_step_service(
    service_name: Optional[str], user_id: str, payload: Optional[Dict[str, Any]] = None
) -> dict:

    # Cria request agnóstico
    request = ServiceRequest(
        service_name=service_name, user_id=user_id, payload=payload or {}
    )

    # Executa via orquestrador agnóstico (async)
    orchestrator = Orchestrator()
    response = await orchestrator.execute_workflow(request)

    # Retorna resposta já formatada
    return response.model_dump()


# Update tool description with available workflows


def save_workflow_graphs():
    """
    Função de conveniência para salvar imagens dos grafos de todos os workflows.

    Returns:
        Dicionário com os resultados da operação
    """
    orchestrator = Orchestrator()
    return orchestrator.save_all_workflow_graphs()


def save_single_workflow_graph(service_name: str):
    """
    Função de conveniência para salvar imagem do grafo de um workflow específico.

    Args:
        service_name: Nome do serviço/workflow

    Returns:
        Caminho para o arquivo de imagem salvo
    """
    orchestrator = Orchestrator()
    return orchestrator.save_workflow_graph_image(service_name)
