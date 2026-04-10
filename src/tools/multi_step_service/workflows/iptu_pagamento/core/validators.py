"""
Validadores reutilizáveis para o workflow IPTU.

Contém funções de validação que podem ser usadas tanto nos modelos Pydantic
quanto diretamente no código do workflow.
"""

import re
from typing import Optional
from src.tools.multi_step_service.workflows.iptu_pagamento.core.constants import (
    ANO_MIN_VALIDO,
    ANO_MAX_VALIDO,
    INSCRICAO_MIN_LENGTH,
    INSCRICAO_MAX_LENGTH,
    INSCRICAO_PATTERN,
)


def validate_and_clean_inscricao(inscricao: str) -> str:
    """
    Valida e sanitiza a inscrição imobiliária.

    Remove caracteres não numéricos e valida comprimento.

    Args:
        inscricao: Inscrição imobiliária (pode conter formatação)

    Returns:
        Inscrição limpa (apenas dígitos)

    Raises:
        ValueError: Se inscrição inválida

    Examples:
        >>> validate_and_clean_inscricao("123.456.78-90")
        '1234567890'
        >>> validate_and_clean_inscricao("1234567890")
        '1234567890'
    """
    # Remove todos os caracteres não numéricos
    clean_inscricao = re.sub(r"[^0-9]", "", inscricao)

    # Valida comprimento
    if len(clean_inscricao) < INSCRICAO_MIN_LENGTH:
        raise ValueError(
            f"Inscrição imobiliária deve ter no mínimo {INSCRICAO_MIN_LENGTH} dígitos"
        )

    if len(clean_inscricao) > INSCRICAO_MAX_LENGTH:
        raise ValueError(
            f"Inscrição imobiliária deve ter no máximo {INSCRICAO_MAX_LENGTH} dígitos"
        )

    return clean_inscricao


def validate_ano_exercicio(ano: int) -> int:
    """
    Valida o ano de exercício.

    Args:
        ano: Ano de exercício fiscal

    Returns:
        Ano validado

    Raises:
        ValueError: Se ano fora do range válido

    Examples:
        >>> validate_ano_exercicio(2025)
        2025
        >>> validate_ano_exercicio(2019)
        Traceback (most recent call last):
        ...
        ValueError: Ano deve estar entre 2020 e 2025
    """
    if ano < ANO_MIN_VALIDO or ano > ANO_MAX_VALIDO:
        raise ValueError(f"Ano deve estar entre {ANO_MIN_VALIDO} e {ANO_MAX_VALIDO}")

    return ano


def validate_numero_guia(numero_guia: str) -> str:
    """
    Valida o número da guia.

    Args:
        numero_guia: Número da guia (ex: "00", "01", "02")

    Returns:
        Número da guia validado

    Raises:
        ValueError: Se número inválido

    Examples:
        >>> validate_numero_guia("00")
        '00'
        >>> validate_numero_guia("01")
        '01'
    """
    # Remove espaços
    numero_clean = numero_guia.strip()

    # Valida que é numérico
    if not numero_clean.isdigit():
        raise ValueError("Número da guia deve conter apenas dígitos")

    # Pad com zeros à esquerda se necessário (aceita "0" como "00")
    if len(numero_clean) == 1:
        numero_clean = numero_clean.zfill(2)

    return numero_clean


def is_inscricao_format_valid(inscricao: str) -> bool:
    """
    Verifica se a inscrição tem formato válido (apenas dígitos, tamanho correto).

    Não levanta exceção, apenas retorna True/False.

    Args:
        inscricao: Inscrição a validar

    Returns:
        True se formato válido, False caso contrário

    Examples:
        >>> is_inscricao_format_valid("12345678")
        True
        >>> is_inscricao_format_valid("123")
        False
    """
    try:
        validate_and_clean_inscricao(inscricao)
        return True
    except ValueError:
        return False


def is_ano_valid(ano: int) -> bool:
    """
    Verifica se o ano é válido.

    Não levanta exceção, apenas retorna True/False.

    Args:
        ano: Ano a validar

    Returns:
        True se ano válido, False caso contrário

    Examples:
        >>> is_ano_valid(2025)
        True
        >>> is_ano_valid(2019)
        False
    """
    try:
        validate_ano_exercicio(ano)
        return True
    except ValueError:
        return False
