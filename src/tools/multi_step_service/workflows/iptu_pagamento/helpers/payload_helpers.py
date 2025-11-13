"""
Helpers para processamento de payloads no workflow IPTU.

Contém funções utilitárias para validação e extração de dados dos payloads recebidos.
"""

from typing import Optional, Any, Type, TypeVar
from pydantic import BaseModel
from src.tools.multi_step_service.core.models import ServiceState

# Type variable for generic Pydantic models
T = TypeVar("T", bound=BaseModel)


def extrair_payload_validado(
    state: ServiceState,
    campo_payload: str,
    modelo_pydantic: Type[T],
) -> Optional[T]:
    """
    Extrai e valida um campo do payload usando modelo Pydantic.

    Args:
        state: Estado do serviço
        campo_payload: Nome do campo no payload a verificar
        modelo_pydantic: Classe Pydantic para validação

    Returns:
        Objeto validado do modelo Pydantic, ou None se campo não existe ou validação falha

    Examples:
        >>> from src.tools.multi_step_service.workflows.iptu_pagamento.models import EscolhaAnoPayload
        >>> state = ServiceState(payload={"ano_exercicio": 2024})
        >>> resultado = extrair_payload_validado(state, "ano_exercicio", EscolhaAnoPayload)
        >>> resultado.ano_exercicio
        2024
    """
    if campo_payload not in state.payload:
        return None

    try:
        return modelo_pydantic.model_validate(state.payload)
    except Exception:
        return None


def salvar_campo_em_data(
    state: ServiceState,
    campo_data: str,
    valor: Any,
) -> None:
    """
    Salva um valor no state.data e limpa agent_response.

    Args:
        state: Estado do serviço
        campo_data: Nome do campo em state.data
        valor: Valor a salvar

    Examples:
        >>> state = ServiceState()
        >>> salvar_campo_em_data(state, "ano_exercicio", 2024)
        >>> state.data["ano_exercicio"]
        2024
        >>> state.agent_response is None
        True
    """
    state.data[campo_data] = valor
    state.agent_response = None


def salvar_campo_em_internal(
    state: ServiceState,
    campo_internal: str,
    valor: Any,
) -> None:
    """
    Salva um valor no state.internal e limpa agent_response.

    Args:
        state: Estado do serviço
        campo_internal: Nome do campo em state.internal
        valor: Valor a salvar

    Examples:
        >>> state = ServiceState()
        >>> salvar_campo_em_internal(state, "darm_separado", True)
        >>> state.internal["darm_separado"]
        True
        >>> state.agent_response is None
        True
    """
    state.internal[campo_internal] = valor
    state.agent_response = None


def processar_payload_simples(
    state: ServiceState,
    campo_payload: str,
    campo_destino: str,
    modelo_pydantic: Type[T],
    usar_internal: bool = False,
) -> bool:
    """
    Processa payload simples: valida e salva em state.data ou state.internal.

    Pattern helper para o caso comum de:
    1. Verificar se campo existe no payload
    2. Validar com Pydantic
    3. Salvar em state.data ou state.internal
    4. Limpar agent_response

    Args:
        state: Estado do serviço
        campo_payload: Nome do campo no payload a procurar
        campo_destino: Nome do campo de destino (data ou internal)
        modelo_pydantic: Classe Pydantic para validação
        usar_internal: Se True, salva em state.internal; se False, salva em state.data

    Returns:
        True se processou com sucesso, False caso contrário

    Examples:
        >>> from src.tools.multi_step_service.workflows.iptu_pagamento.models import EscolhaAnoPayload
        >>> state = ServiceState(payload={"ano_exercicio": 2024})
        >>> processar_payload_simples(state, "ano_exercicio", "ano_exercicio", EscolhaAnoPayload)
        True
        >>> state.data["ano_exercicio"]
        2024
    """
    validado = extrair_payload_validado(state, campo_payload, modelo_pydantic)

    if validado is None:
        return False

    # Extrai o valor do campo do objeto validado
    valor = getattr(validado, campo_payload)

    if usar_internal:
        salvar_campo_em_internal(state, campo_destino, valor)
    else:
        salvar_campo_em_data(state, campo_destino, valor)

    return True


def campo_ja_existe(
    state: ServiceState, campo: str, usar_internal: bool = False
) -> bool:
    """
    Verifica se um campo já existe em state.data ou state.internal.

    Args:
        state: Estado do serviço
        campo: Nome do campo a verificar
        usar_internal: Se True, verifica em state.internal; se False, verifica em state.data

    Returns:
        True se campo existe, False caso contrário

    Examples:
        >>> state = ServiceState(data={"inscricao": "123"})
        >>> campo_ja_existe(state, "inscricao")
        True
        >>> campo_ja_existe(state, "ano")
        False
        >>> state.internal["flag"] = True
        >>> campo_ja_existe(state, "flag", usar_internal=True)
        True
    """
    if usar_internal:
        return campo in state.internal
    return campo in state.data
