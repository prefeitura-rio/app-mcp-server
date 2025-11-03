"""
Helpers para manipulação de state no workflow IPTU.

Contém funções utilitárias para validação e reset de dados no ServiceState.
"""

from typing import Optional, List, Dict
from src.tools.multi_step_service.core.models import ServiceState
from src.tools.multi_step_service.workflows.iptu_pagamento.helpers import utils


def validar_dados_obrigatorios(state: ServiceState, campos: List[str]) -> Optional[str]:
    """
    Valida se campos obrigatórios existem no state.data.

    Args:
        state: Estado do serviço
        campos: Lista de campos obrigatórios a validar

    Returns:
        None se todos os campos existem, ou nome do primeiro campo faltante

    Examples:
        >>> state = ServiceState(data={"inscricao": "123"})
        >>> validar_dados_obrigatorios(state, ["inscricao"])
        None
        >>> validar_dados_obrigatorios(state, ["inscricao", "ano"])
        'ano'
    """
    for campo in campos:
        if campo not in state.data or state.data[campo] is None:
            return campo
    return None


def reset_completo(
    state: ServiceState,
    manter_inscricao: bool = False,
    fields: Optional[Dict[str, List[str]]] = None,
) -> None:
    """
    Faz reset completo ou seletivo dos dados e flags internas.

    Args:
        state: Estado do serviço
        manter_inscricao: Se True, mantém a inscrição imobiliária atual
        fields: Dict com 'data' e 'internal' contendo listas de campos para resetar.
               Se None, faz reset completo. Se especificado, reseta apenas os campos listados.

    Examples:
        >>> state = ServiceState(data={"inscricao": "123", "ano": 2024})
        >>> reset_completo(state, manter_inscricao=False)
        >>> state.data
        {}

        >>> state = ServiceState(data={"inscricao": "123", "ano": 2024})
        >>> reset_completo(state, manter_inscricao=True)
        >>> state.data
        {'inscricao_imobiliaria': '123'}

        >>> state = ServiceState(data={"a": 1, "b": 2, "c": 3})
        >>> reset_completo(state, fields={"data": ["b", "c"], "internal": []})
        >>> state.data
        {'a': 1}
    """
    if fields is None:
        utils.reset_completo(state, manter_inscricao)
    else:
        utils.reset_campos_seletivo(state, fields, manter_inscricao)


def reset_para_selecao_cotas(state: ServiceState) -> None:
    """
    Reset específico para voltar à seleção de cotas.

    Remove dados de cotas escolhidas, DARM e formato de boleto.
    Útil quando há erro na geração de boletos.

    Args:
        state: Estado do serviço

    Examples:
        >>> state = ServiceState(data={
        ...     "inscricao": "123",
        ...     "cotas_escolhidas": ["01", "02"],
        ...     "dados_darm": {...}
        ... })
        >>> reset_para_selecao_cotas(state)
        >>> "cotas_escolhidas" in state.data
        False
        >>> "inscricao" in state.data
        True
    """
    state.data.pop("cotas_escolhidas", None)
    state.data.pop("dados_darm", None)
    state.internal.pop("darm_separado", None)
    state.internal.pop("dados_confirmados", None)
