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

    Quando a mensagem menciona os termos abaixo com intenção de ação (não apenas
    informação), **CHAME esta tool IMEDIATAMENTE** e **SEMPRE extraia e passe os dados
    mencionados pelo usuário no payload**:

    - IPTU: "pagar IPTU", "emitir guia de IPTU", "guia de IPTU", "consultar débitos",
      ou número de 7-8 dígitos após consulta de IPTU → service_name="iptu_pagamento"
      (apenas IPTU — NÃO usar pra outras guias como ISS, ITBI, taxa de lixo)
    - Dívida Ativa: QUALQUER menção a "dívida ativa", "débitos", "CDA", "certidão
      de dívida", "execução fiscal", "parcelar dívida" — mesmo que a mensagem seja
      curta, vaga ou pareça apenas informacional — chame `build_whatsapp_flow_envelope`
      com service_type="divida_ativa" IMEDIATAMENTE como única ação do turno.
      NÃO gere texto, NÃO faça perguntas, NÃO mencione gov.br nem autenticação.
      O Flow cuida de tudo. Chame multi_step_service só DEPOIS que o cidadão
      submeter o Flow (inbound com _source="whatsapp_flow").
    - Luminária pública / poste / iluminação → reparo_luminaria.
      ⚠️ EXCEÇÃO DE ORDEM: pra luminária, o PRIMEIRO passo NÃO é esta tool — chame
      `build_whatsapp_flow_envelope` (Flow prefillado) ANTES; o Flow é a etapa de
      confirmação dos dados (mande defeito/quantidade/local que o cidadão já disse no
      `prefill_data`). Chame multi_step_service(service_name="reparo_luminaria") só
      DEPOIS que o cidadão submeter o Flow (inbound com _source="whatsapp_flow"), pra
      processar a submissão, coletar o endereço e abrir o chamado. Não deflexione pra
      google_search nem diga "não consigo".
    - Poda de árvore (via pública) → service_name="poda_de_arvore"

    **NÃO chame** esta tool (use `google_search` ou responda informacionalmente) quando:
    - Mensagem for informacional ("quero saber o calendário do IPTU", "como funciona a poda")
    - Termo for sobre questão privada ("trocar lâmpada da minha casa", "podar árvore do meu quintal")
    - Mensagem for vaga sem intenção clara de abrir solicitação
    EXCEÇÃO: dívida ativa nunca se enquadra nas regras acima — sempre chame
    `build_whatsapp_flow_envelope`, mesmo que a mensagem seja vaga ou curta.

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

    **🚨 REGRA CRÍTICA — RETORNO DE GUIA DE PAGAMENTO (divida_ativa) 🚨**

    Quando `multi_step_service` retornar `description` contendo uma guia emitida
    (ex: "Guia de pagamento gerada com sucesso"), você DEVE repassar o conteúdo da
    `description` **LITERALMENTE e NA ÍNTEGRA** ao cidadão.
    O sistema já pergunta ao cidadão qual forma de pagamento ele deseja (botões:
    Boleto bancário / Código de barras / Pix copia-e-cola). Quando o cidadão
    escolher, repasse o dado **LITERALMENTE** (link, código de barras ou código PIX)
    sem parafrasear, resumir ou omitir.

    Args:
        service_name: Nome do serviço (ex: "bank_account")
        payload: Dicionário com campos solicitados no payload_schema.
        user_id: ID do agente, passar sempre 'agent'

    **⚠️ IMPORTANTE - NÃO USE MEMÓRIA AUTOMATICAMENTE:**
    - **NUNCA** preencha o payload inicial com dados de `get_user_memory` sem confirmar com o usuário
    - Memórias podem estar desatualizadas ou de contextos diferentes
    - **SEMPRE pergunte** ao usuário antes de usar dados salvos em memória
    - Exemplo ERRADO: usuário diz "pagar IPTU" → você chama get_user_memory → preenche inscricao_imobiliaria automaticamente ❌
    - Exemplo CORRETO: usuário diz "pagar IPTU" → você chama multi_step_service(payload={}) → sistema pede inscrição → usuário informa ✅

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
