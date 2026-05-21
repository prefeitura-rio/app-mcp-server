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

    **🚨 PRIORIDADE MÁXIMA — ROTEAMENTO OBRIGATÓRIO 🚨**

    Quando a mensagem do cidadão expressa intenção de **abrir solicitação ao serviço
    público municipal** (verbos: `solicitar`, `abrir chamado`, `iniciar reparo`,
    `iniciar atendimento`, `pedir reparo`, `pedir poda`, `emitir guia`, `pagar IPTU`)
    + um dos termos abaixo, você **DEVE chamar esta tool IMEDIATAMENTE**:

    - "luminária pública" / "poste apagado" / "iluminação pública" / "lâmpada da rua/poste"
      → service_name="reparo_luminaria"
    - "pagar IPTU" / "emitir guia IPTU" / "consultar débitos IPTU"
      → service_name="iptu_pagamento"
    - "podar árvore (da via pública/calçada/rua)" / "solicitar poda"
      → service_name="poda_de_arvore"

    **NÃO chame** esta tool (use `google_search` ou responda informacionalmente) quando:
    - Mensagem for informacional ("quero saber o calendário do IPTU", "como funciona a poda")
    - Termo for sobre questão privada ("trocar lâmpada da minha casa", "podar árvore do meu quintal")
    - Mensagem for vaga sem intenção clara de abrir solicitação

    Verifique a lista de workflows registrados abaixo antes de chamar; use exatamente
    os `service_name` da seção "serviços disponíveis" listada mais abaixo.

    **PROIBIDO:**
    - Usar `google_search` antes desta tool quando a mensagem bater com termos acima.
    - Responder texto sobre canais 1746 / Carioca Digital quando há workflow disponível aqui.
    - Dizer ao cidadão que "não consigo fazer essa solicitação" — esta tool consegue.

    Se a tool retornar `error_message` indicando serviço indisponível, aí sim use
    fallback (`google_search`).

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
