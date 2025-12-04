"""
Executor para workflows baseados em hooks.

Este módulo implementa o FlowExecutor, responsável por executar workflows
proceduralmente e detectar navegação não-linear automaticamente.
"""

from typing import Dict, Any
from loguru import logger

from src.tools.multi_step_service.core.models import ServiceState, AgentResponse
from src.tools.multi_hook_services.core.base_flow import BaseFlow
from src.tools.multi_hook_services.core.flow_exceptions import (
    FlowPause,
    FlowError,
    FlowCancelled
)


class FlowExecutor:
    """
    Executor de workflows baseados em hooks.

    Responsabilidades:
    - Executar método run() do flow proceduralmente
    - Capturar FlowPause exceptions para pausar execução
    - Detectar e resetar navegação não-linear automaticamente
    - Tratar erros de forma consistente

    Example:
        executor = FlowExecutor()
        flow = IPTUFlow(state)
        final_state = await executor.execute(flow, state, payload)
    """

    async def execute(
        self,
        flow: BaseFlow,
        state: ServiceState,
        payload: Dict[str, Any]
    ) -> ServiceState:
        """
        Executa um flow de forma procedural.

        Args:
            flow: Instância do flow a executar
            state: Estado do serviço
            payload: Dados da requisição atual

        Returns:
            Estado atualizado com agent_response definido
        """
        # Injeta payload no estado
        flow.state = state
        flow.state.payload = payload or {}

        logger.info(f"FlowExecutor: executando {flow.service_name} para user_id={state.user_id}")

        # Detecta navegação não-linear e faz reset automático
        self._detect_and_reset_navigation(flow)

        try:
            # Executa workflow proceduralmente
            logger.debug("FlowExecutor: chamando flow.run()")
            agent_response = await flow.run()

            # Workflow completou com sucesso
            state.agent_response = agent_response
            state.status = "completed"

            logger.info(f"FlowExecutor: workflow {flow.service_name} completado com sucesso")

        except FlowPause as pause:
            # Workflow pausou para pedir input do usuário
            state.agent_response = pause.response
            state.status = "progress"

            logger.debug(f"FlowExecutor: workflow pausado, aguardando input para campo no payload_schema")

        except FlowCancelled as cancelled:
            # Workflow foi cancelado pelo usuário
            state.agent_response = AgentResponse(
                service_name=flow.service_name,
                description=cancelled.message,
                payload_schema=None,
                data=state.data
            )
            state.status = "completed"

            logger.info(f"FlowExecutor: workflow {flow.service_name} cancelado pelo usuário")

        except FlowError as flow_error:
            # Erro de negócio/validação no workflow
            state.agent_response = AgentResponse(
                service_name=flow.service_name,
                description=flow_error.message,
                payload_schema=None,
                error_message=flow_error.detail,
                data=state.data
            )
            state.status = "error"

            logger.error(f"FlowExecutor: erro no workflow = {flow_error.detail}")

        except Exception as e:
            # Erro inesperado - falha do sistema
            error_msg = f"Erro interno: {str(e)}"
            state.agent_response = AgentResponse(
                service_name=flow.service_name,
                description="Ocorreu um erro interno no sistema. Por favor, tente novamente.",
                payload_schema=None,
                error_message=error_msg,
                data=state.data
            )
            state.status = "error"

            logger.exception(f"FlowExecutor: erro inesperado no workflow {flow.service_name}")

        # Limpa payload (dados temporários)
        state.payload = {}

        return state

    def _detect_and_reset_navigation(self, flow: BaseFlow) -> None:
        """
        Detecta navegação não-linear e faz reset automático.

        Navegação não-linear ocorre quando o usuário envia um campo que já foi
        coletado anteriormente (está no _step_stack). Quando detectado:
        1. Remove todos os steps posteriores da pilha
        2. Remove dados desses steps posteriores de state.data
        3. Remove cache de API relacionado

        Args:
            flow: Instância do flow

        Example:
            step_stack = ["inscricao", "ano", "guia", "cotas"]
            payload = {"ano": 2024}  # Usuário voltou para step 2

            Resultado:
            - step_stack = ["inscricao", "ano"]
            - Remove de state.data: "guia", "cotas"
            - Remove cache relacionado
        """
        payload_keys = set(flow.state.payload.keys())

        # Verifica se algum campo do payload está no step_stack
        # (indicando que é um step anterior que está sendo revisitado)
        for i in range(len(flow._step_stack) - 1, -1, -1):
            step_field = flow._step_stack[i]

            if step_field in payload_keys:
                # Encontrou navegação não-linear!
                logger.info(
                    f"FlowExecutor: navegação não-linear detectada - "
                    f"campo '{step_field}' de step anterior no payload"
                )

                # Remove steps posteriores da pilha
                removed_steps = flow._step_stack[i+1:]
                flow._step_stack = flow._step_stack[:i+1]

                if removed_steps:
                    logger.debug(f"FlowExecutor: removendo steps posteriores: {removed_steps}")

                    # Remove dados dos steps posteriores
                    for removed_field in removed_steps:
                        if removed_field in flow.state.data:
                            flow.state.data.pop(removed_field)
                            logger.debug(f"FlowExecutor: removido campo '{removed_field}' de state.data")

                    # Remove cache de API relacionado aos steps removidos
                    cache_keys_to_remove = [
                        k for k in flow.state.internal.keys()
                        if k.startswith("api_cache_") and any(f in k for f in removed_steps)
                    ]

                    for cache_key in cache_keys_to_remove:
                        flow.state.internal.pop(cache_key, None)
                        logger.debug(f"FlowExecutor: removido cache '{cache_key}'")

                # Para após encontrar o primeiro (evita múltiplos resets)
                break
