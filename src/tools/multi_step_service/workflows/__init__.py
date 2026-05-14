"""
Workflows V5 - LangGraph based multi-step services

Este módulo centraliza todos os workflows disponíveis.
Para adicionar um novo workflow:
1. Crie o arquivo do workflow em workflows/
2. Importe e adicione na lista 'workflows' abaixo
3. Cada workflow deve ter um atributo 'service_name'
"""

# Import workflows aqui
from src.tools.multi_step_service.workflows.iptu_pagamento import IPTUWorkflow
from src.tools.multi_step_service.workflows.poda_de_arvore.workflow import (
    PodaDeArvoreWorkflow,
)
from src.tools.multi_step_service.workflows.reparo_luminaria.workflow import (
    ReparoLuminariaWorkflow,
)

# Lista central de workflows (classes)
workflows = [
    IPTUWorkflow,
    PodaDeArvoreWorkflow,
    ReparoLuminariaWorkflow,
]

# Lista de workflows disponíveis para import fácil
__all__ = ["workflows"]
