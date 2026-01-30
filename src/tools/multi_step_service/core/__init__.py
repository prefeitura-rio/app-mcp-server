"""
Core components do framework multi-step service.
"""

from src.tools.multi_step_service.core.base_workflow import BaseWorkflow, handle_errors
from src.tools.multi_step_service.core.step_navigator import StepNavigator
from src.tools.multi_step_service.core.models import (
    ServiceState,
    AgentResponse,
    ServiceRequest,
)
from src.tools.multi_step_service.core.state import StateManager, StateMode
from src.tools.multi_step_service.core.orchestrator import Orchestrator

DESCRIPTION = """
    Sistema de serviços multi-step com gerenciamento de estado e navegação não-linear.
    
    **IMPORTANTE!! Esta tool funciona APENAS para os seguintes serviços:**
    
    __replace__available_services__
    
    Args:
        service_name: Nome do serviço (ex: "bank_account")
        payload: Dicionário com campos solicitados no payload_schema.
        user_id: ID do agente, passar sempre 'agent'

    COMO PREENCHER O PAYLOAD:
    Este sistema gerencia o estado da conversa. Sua decisão sobre o que enviar no `payload` define o comportamento do fluxo:

    1. FLUXO SEQUENCIAL (Comportamento Padrão):
       - O sistema fornece um `payload_schema` indicando o que é necessário para a etapa atual.
       - Se o usuário responder a pergunta atual, envie apenas o campo solicitado.

    2. FLUXO DE CORREÇÃO (Navegação/Rollback):
       - O usuário pode mudar de ideia sobre uma informação já fornecida em etapas anteriores.
       - Se o usuário corrigir um dado passado (ex: mudar a data, o tipo de serviço, etc.), **envie este campo no payload**, ignorando o schema da etapa atual.
       - O sistema detectará que é um campo anterior, resetará o fluxo para aquele ponto e limpará as etapas dependentes.

    Exemplo Genérico (Contexto: Agendamento):
    
    [Cenário A - Seguindo o fluxo]
        - O sistema pede: "data_agendamento" (Etapa 2)
        - Usuário responde: "Dia 25 de outubro"
        - Ação: Envie {"data_agendamento": "2025-10-25"}

    [Cenário B - Correção de etapa anterior]
        - O sistema pede: "horario_disponivel" (Etapa 3)
        - Usuário responde: "Espere, quero mudar a especialidade para Cardiologia" (Dado da Etapa 1)
        - Ação: Envie {"especialidade": "cardiologia"}
        * O sistema voltará automaticamente para a Etapa 1 e pedirá a data novamente depois.
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


tools_description = _get_workflow_descriptions()

__all__ = [
    "BaseWorkflow",
    "handle_errors",
    "StepNavigator",
    "ServiceState",
    "AgentResponse",
    "ServiceRequest",
    "StateManager",
    "StateMode",
    "Orchestrator",
    "tools_description",
]
