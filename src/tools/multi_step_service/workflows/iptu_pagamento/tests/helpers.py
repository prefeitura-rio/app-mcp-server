"""
Test helpers para testes do workflow IPTU.

Fornece fixtures e funções utilitárias reutilizáveis para testes.
"""

import os
import time
from typing import Dict, Any, Optional


# Dados de teste reutilizáveis
INSCRICAO_VALIDA = "01234567890123"  # Válida na API fake
INSCRICAO_SEM_GUIAS = "99999999999999"  # Sem guias na API fake
INSCRICAO_INVALIDA = "123"  # Formato inválido
ANO_VALIDO = 2025
ANO_INVALIDO = 2019


def setup_fake_api() -> None:
    """
    Configura variável de ambiente para forçar uso da API fake.
    Deve ser chamado antes de cada teste.

    Examples:
        >>> setup_fake_api()
        >>> os.getenv("IPTU_USE_FAKE_API")
        'true'
    """
    os.environ["IPTU_USE_FAKE_API"] = "true"


def teardown_fake_api() -> None:
    """
    Remove configuração da API fake após o teste.

    Examples:
        >>> setup_fake_api()
        >>> teardown_fake_api()
        >>> os.getenv("IPTU_USE_FAKE_API")
        None
    """
    os.environ.pop("IPTU_USE_FAKE_API", None)


def gerar_user_id() -> str:
    """
    Gera um user_id único para testes.

    Returns:
        String única baseada em timestamp

    Examples:
        >>> user_id = gerar_user_id()
        >>> user_id.startswith("test_user_")
        True
    """
    return f"test_user_{int(time.time() * 1000000)}"


def criar_payload_inscricao(inscricao: str = INSCRICAO_VALIDA) -> Dict[str, str]:
    """
    Cria payload para informar inscrição imobiliária.

    Args:
        inscricao: Inscrição a usar (padrão: INSCRICAO_VALIDA)

    Returns:
        Dict com payload formatado

    Examples:
        >>> criar_payload_inscricao()
        {'inscricao_imobiliaria': '01234567890123'}
    """
    return {"inscricao_imobiliaria": inscricao}


def criar_payload_ano(ano: int = ANO_VALIDO) -> Dict[str, int]:
    """
    Cria payload para escolher ano de exercício.

    Args:
        ano: Ano a usar (padrão: ANO_VALIDO)

    Returns:
        Dict com payload formatado

    Examples:
        >>> criar_payload_ano()
        {'ano_exercicio': 2025}
    """
    return {"ano_exercicio": ano}


def criar_payload_guia(numero_guia: str = "00") -> Dict[str, str]:
    """
    Cria payload para escolher guia.

    Args:
        numero_guia: Número da guia (padrão: "00" - ordinária)

    Returns:
        Dict com payload formatado

    Examples:
        >>> criar_payload_guia()
        {'guia_escolhida': '00'}
        >>> criar_payload_guia("01")
        {'guia_escolhida': '01'}
    """
    return {"guia_escolhida": numero_guia}


def criar_payload_cotas(cotas: list[str]) -> Dict[str, list[str]]:
    """
    Cria payload para escolher cotas.

    Args:
        cotas: Lista de números de cotas

    Returns:
        Dict com payload formatado

    Examples:
        >>> criar_payload_cotas(["01", "02"])
        {'cotas_escolhidas': ['01', '02']}
    """
    return {"cotas_escolhidas": cotas}


def criar_payload_confirmacao(confirma: bool = True) -> Dict[str, bool]:
    """
    Cria payload para confirmação de dados.

    Args:
        confirma: Se confirma ou não (padrão: True)

    Returns:
        Dict com payload formatado

    Examples:
        >>> criar_payload_confirmacao()
        {'confirmacao': True}
        >>> criar_payload_confirmacao(False)
        {'confirmacao': False}
    """
    return {"confirmacao": confirma}


def criar_payload_mais_cotas(quer_mais: bool = False) -> Dict[str, bool]:
    """
    Cria payload para pergunta sobre mais cotas.

    Args:
        quer_mais: Se quer pagar mais cotas (padrão: False)

    Returns:
        Dict com payload formatado

    Examples:
        >>> criar_payload_mais_cotas()
        {'mais_cotas': False}
    """
    return {"mais_cotas": quer_mais}


def criar_payload_outras_guias(quer_outras: bool = False) -> Dict[str, bool]:
    """
    Cria payload para pergunta sobre outras guias.

    Args:
        quer_outras: Se quer outras guias (padrão: False)

    Returns:
        Dict com payload formatado

    Examples:
        >>> criar_payload_outras_guias()
        {'outras_guias': False}
    """
    return {"outras_guias": quer_outras}


def criar_payload_outro_imovel(quer_outro: bool = False) -> Dict[str, bool]:
    """
    Cria payload para pergunta sobre outro imóvel.

    Args:
        quer_outro: Se quer outro imóvel (padrão: False)

    Returns:
        Dict com payload formatado

    Examples:
        >>> criar_payload_outro_imovel()
        {'outro_imovel': False}
    """
    return {"outro_imovel": quer_outro}


def criar_payload_darm_separado(separado: bool = False) -> Dict[str, bool]:
    """
    Cria payload para escolha de formato de DARM.

    Args:
        separado: Se quer DARMs separados (padrão: False)

    Returns:
        Dict com payload formatado

    Examples:
        >>> criar_payload_darm_separado()
        {'darm_separado': False}
    """
    return {"darm_separado": separado}


def criar_request(
    service_name: str,
    user_id: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Cria estrutura de request para o multi_step_service.

    Args:
        service_name: Nome do serviço
        user_id: ID do usuário
        payload: Payload a enviar

    Returns:
        Dict com request completo

    Examples:
        >>> criar_request("iptu_pagamento", "user_123", {"ano": 2025})
        {'service_name': 'iptu_pagamento', 'user_id': 'user_123', 'payload': {'ano': 2025}}
    """
    return {
        "service_name": service_name,
        "user_id": user_id,
        "payload": payload,
    }


def verificar_response_tem_schema(response: Dict[str, Any]) -> bool:
    """
    Verifica se response tem payload_schema.

    Args:
        response: Response do multi_step_service

    Returns:
        True se tem schema, False caso contrário

    Examples:
        >>> verificar_response_tem_schema({"payload_schema": {}})
        True
        >>> verificar_response_tem_schema({"payload_schema": None})
        False
    """
    return response.get("payload_schema") is not None


def verificar_response_sem_erro(response: Dict[str, Any]) -> bool:
    """
    Verifica se response não tem erro.

    Args:
        response: Response do multi_step_service

    Returns:
        True se não tem erro, False caso contrário

    Examples:
        >>> verificar_response_sem_erro({"error_message": None})
        True
        >>> verificar_response_sem_erro({"error_message": "Erro!"})
        False
    """
    return response.get("error_message") is None


def verificar_response_com_erro(response: Dict[str, Any]) -> bool:
    """
    Verifica se response tem erro.

    Args:
        response: Response do multi_step_service

    Returns:
        True se tem erro, False caso contrário

    Examples:
        >>> verificar_response_com_erro({"error_message": "Erro!"})
        True
        >>> verificar_response_com_erro({"error_message": None})
        False
    """
    return response.get("error_message") is not None


def extrair_campo_schema(response: Dict[str, Any], campo: str) -> bool:
    """
    Verifica se campo específico existe no schema da response.

    Args:
        response: Response do multi_step_service
        campo: Nome do campo a procurar

    Returns:
        True se campo existe no schema, False caso contrário

    Examples:
        >>> response = {"payload_schema": {"properties": {"inscricao": {}}}}
        >>> extrair_campo_schema(response, "inscricao")
        True
        >>> extrair_campo_schema(response, "ano")
        False
    """
    schema = response.get("payload_schema")
    if not schema:
        return False
    properties = schema.get("properties", {})
    return campo in properties
