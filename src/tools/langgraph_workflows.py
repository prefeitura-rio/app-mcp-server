from typing import Any, Dict, Optional

from src.tools.multi_step_service.core.orchestrator import Orchestrator
from src.tools.multi_step_service.core.models import ServiceRequest
from src.tools.multi_step_service.core.state import StateMode

DESCRIPTION = """
    Sistema de serviços multi-step com schema dinâmico e estado transparente.

    Args:
        service_name: Nome do serviço (ex: "bank_account")
        payload: Dicionário com campos solicitados no payload_schema. **Envie apenas o que for solicidado na etapa atual!!**.
        user_id: ID do agente, passar sempre 'agent'

    IMPORTANTE: Este serviço funciona em ETAPAS SEQUENCIAIS.
    - Cada etapa solicita campos específico no payload_schema
    - Você DEVE enviar SOMENTE o campo solicitado na etapa atual
    - NÃO inclua campos de etapas anteriores no payload
    - O sistema já armazena os dados das etapas anteriores automaticamente

    Exemplo CORRETO:
    - Etapa 1 pede "nome" → envie {"nome": "..."}
    - Etapa 2 pede "email" → envie {"email": "..."} (SEM nome)
    - Etapa 3 pede "idade e endereco" → envie {"idade": ..., "endereco":"..."} (SEM campos anteriores)

    Exemplo INCORRETO (NÃO FAÇA ISSO):
    - Etapa 2: {"nome": "...", "email": "..."} ❌ ERRADO
    - Etapa 3: {"nome": "...", "email": "...", "idade": ..., "endereco":"..."} ❌ ERRADO"

    Serviços disponíveis:
        - service_name: description

        __replace__available_services__
    """


def _get_workflow_descriptions():
    """Generate workflow descriptions for the tool docstring"""
    orchestrator = Orchestrator()
    workflow_dict = orchestrator.list_workflows()

    if not workflow_dict:
        return "- Nenhum workflow disponível"

    descriptions = []
    for service_name, description in workflow_dict.items():
        descriptions.append(f"- {service_name}: {description}")

    description_replacer = "\n        ".join(descriptions)

    return DESCRIPTION.replace("__replace__available_services__", description_replacer)


async def multi_step_service(
    service_name: str, user_id: str, payload: Optional[Dict[str, Any]] = None
) -> dict:

    # Cria request agnóstico
    request = ServiceRequest(
        service_name=service_name, user_id=user_id, payload=payload or {}
    )

    # Executa via orquestrador agnóstico (async)
    orchestrator = Orchestrator(backend_mode=StateMode.REDIS)
    response = await orchestrator.execute_workflow(request)

    # Retorna resposta já formatada
    return response.model_dump()


def save_workflow_graphs():
    """
    Função de conveniência para salvar imagens dos grafos de todos os workflows.

    Returns:
        Dicionário com os resultados da operação
    """
    orchestrator = Orchestrator(backend_mode=StateMode.REDIS)
    return orchestrator.save_all_workflow_graphs()


def save_single_workflow_graph(service_name: str):
    """
    Função de conveniência para salvar imagem do grafo de um workflow específico.

    Args:
        service_name: Nome do serviço/workflow

    Returns:
        Caminho para o arquivo de imagem salvo
    """
    orchestrator = Orchestrator(backend_mode=StateMode.REDIS)
    return orchestrator.save_workflow_graph_image(service_name)
