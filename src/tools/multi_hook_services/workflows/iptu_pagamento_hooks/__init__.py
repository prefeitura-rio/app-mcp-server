"""
IPTU Workflow - Versão Hooks-Based (POC)

Esta é uma implementação alternativa do workflow IPTU usando o framework hooks-based,
demonstrando redução de código de 992 linhas (LangGraph) para ~300 linhas (hooks).

Comparação:
- Versão LangGraph: 992 linhas (iptu_workflow.py)
- Versão Hooks: ~300 linhas (iptu_flow.py)
- Redução: ~66% de código

Vantagens:
- Código procedural (linear) em vez de grafo
- Navegação não-linear automática
- Menos boilerplate (2-3 linhas por input vs 50-100)
- Debugging mais fácil (stack traces lineares)
- Sem dependência de LangGraph
"""

from src.tools.multi_hook_services.workflows.iptu_pagamento_hooks.iptu_flow import IPTUFlow

__all__ = ["IPTUFlow"]
