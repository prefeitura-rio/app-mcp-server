"""
Exceções para controle de fluxo do framework hooks-based.

Este módulo define as exceções customizadas usadas para controlar
o fluxo de execução de workflows baseados em hooks.
"""

from src.tools.multi_step_service.core.models import AgentResponse


class FlowPause(Exception):
    """
    Exceção lançada quando o workflow precisa pausar para coletar input do usuário.

    Esta é a exceção principal do framework hooks-based. Quando um hook como
    use_input() ou use_choice() precisa de dados que não estão disponíveis,
    ele levanta FlowPause com a resposta apropriada para o agente.

    O FlowExecutor captura essa exceção e retorna o AgentResponse,
    pausando a execução até a próxima requisição do usuário.

    Args:
        response: AgentResponse com a descrição e payload_schema para o usuário
    """

    def __init__(self, response: AgentResponse):
        self.response = response
        super().__init__(response.description)


class FlowError(Exception):
    """
    Exceção lançada quando ocorre um erro irrecuperável no workflow.

    Esta exceção deve ser usada para erros que impedem a continuação
    do workflow, como falhas de validação críticas ou erros de negócio.

    Args:
        message: Mensagem de erro para o usuário
        detail: Detalhes técnicos do erro (opcional)
    """

    def __init__(self, message: str, detail: str = ""):
        self.message = message
        self.detail = detail
        super().__init__(message)


class FlowCancelled(Exception):
    """
    Exceção lançada quando o usuário cancela o workflow explicitamente.

    Esta exceção indica que o usuário optou por não continuar com o workflow,
    normalmente durante uma confirmação ou escolha.

    Args:
        message: Mensagem de cancelamento para o usuário
    """

    def __init__(self, message: str = "Operação cancelada pelo usuário"):
        self.message = message
        super().__init__(message)
