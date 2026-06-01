from typing import Any, Dict, Optional

from loguru import logger

from src.tools.multi_step_service.core import (
    Orchestrator,
    ServiceRequest,
    StateMode,
    tools_description,
)
from src.tools.multi_step_service.core.state import StateManager

from src.config import env

if env.IS_LOCAL:
    BACKEND_MODE = StateMode.JSON
else:
    BACKEND_MODE = StateMode.REDIS

__all__ = [
    "multi_step_service",
    "reset_session_state",
    "save_workflow_graphs",
    "save_single_workflow_graph",
    "tools_description",
]


async def multi_step_service(
    service_name: str, user_id: str, payload: Optional[Dict[str, Any]] = None
) -> dict:
    # Cria request agnóstico
    request = ServiceRequest(
        service_name=service_name, user_id=user_id, payload=payload or {}
    )

    # Executa via orquestrador agnóstico (async)
    orchestrator = Orchestrator(backend_mode=BACKEND_MODE)
    response = await orchestrator.execute_workflow(request)

    # Retorna resposta já formatada
    return response.model_dump()


async def reset_session_state(
    user_id: str, backend_mode: Optional[StateMode] = None
) -> dict:
    """Encerra o atendimento: limpa TODO o estado de workflow multi-step do
    cidadão (luminária, poda, IPTU…) para o telefone do thread.

    Segurança: o ``user_id`` que chega aqui é o do thread autenticado. O engine
    sobrescreve qualquer ``user_id`` que o modelo passe na tool-call pelo
    ``thread_id`` (ver ``engine/agent.py::_inject_thread_id_in_user_id_params``,
    genérico para todas as tools e param ``user_id``/``user_number``), então o
    modelo NÃO controla o alvo do reset — mesma garantia do ``multi_step_service``.
    """
    # A construção do StateManager fica DENTRO do try: com backend Redis/BOTH, a
    # criação do backend pode falhar (config inválida/dependência indisponível)
    # antes mesmo do delete — sem isso a falha escaparia como ToolError em vez de
    # degradar graciosamente.
    try:
        state_manager = StateManager(
            user_id=user_id, backend_mode=backend_mode or BACKEND_MODE
        )
        cleared = await state_manager.remove_user_data()
    except Exception as exc:
        # Convenção do repo (Orchestrator / reverse_geocode_address): capturar e
        # devolver erro estruturado em vez de propagar. Re-tentável no próximo
        # turno — o reset é idempotente.
        logger.opt(exception=True).warning(
            "reset_session_state falhou ({}): {}", type(exc).__name__, exc
        )
        return {"status": "error", "cleared": False}
    return {"status": "ok", "cleared": bool(cleared)}


def save_workflow_graphs():
    """
    Função de conveniência para salvar imagens dos grafos de todos os workflows.

    Returns:
        Dicionário com os resultados da operação
    """
    orchestrator = Orchestrator(backend_mode=BACKEND_MODE)
    return orchestrator.save_all_workflow_graphs()


def save_single_workflow_graph(service_name: str):
    """
    Função de conveniência para salvar imagem do grafo de um workflow específico.

    Args:
        service_name: Nome do serviço/workflow

    Returns:
        Caminho para o arquivo de imagem salvo
    """
    orchestrator = Orchestrator(backend_mode=BACKEND_MODE)
    return orchestrator.save_workflow_graph_image(service_name)
