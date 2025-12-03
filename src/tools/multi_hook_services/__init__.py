"""
Multi-Hook Services - Framework hooks-based para workflows multi-step.

Este framework simplifica drasticamente a criação de workflows conversacionais,
reduzindo código em ~10x através de hooks inspirados em React.

Example:
    from src.tools.multi_hook_services import BaseFlow, FlowExecutor

    class MeuFlow(BaseFlow):
        service_name = "meu_servico"
        description = "Descrição"

        async def run(self) -> AgentResponse:
            nome = await self.use_input("nome", NomePayload, "Seu nome:")
            return self.success("Sucesso!", {"nome": nome})

    # Executar
    executor = FlowExecutor()
    state = await executor.execute(flow, state, payload)
"""

from src.tools.multi_hook_services.core.base_flow import BaseFlow
from src.tools.multi_hook_services.core.flow_executor import FlowExecutor
from src.tools.multi_hook_services.core.flow_exceptions import (
    FlowPause,
    FlowError,
    FlowCancelled
)

__all__ = [
    "BaseFlow",
    "FlowExecutor",
    "FlowPause",
    "FlowError",
    "FlowCancelled",
]

__version__ = "1.0.0-poc"
