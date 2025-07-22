"""
Ferramentas de calculadora básica para o servidor FastMCP.
"""

from typing import Union
from src.config.settings import FEATURES_CONFIG


def add(a: float, b: float) -> float:
    """
    Soma dois números.

    Args:
        a: Primeiro número
        b: Segundo número

    Returns:
        Resultado da soma
    """
    precision = FEATURES_CONFIG["calculator"]["precision"]
    result = a + b
    return round(result, precision)


def subtract(a: float, b: float) -> float:
    """
    Subtrai dois números.

    Args:
        a: Primeiro número
        b: Segundo número

    Returns:
        Resultado da subtração (a - b)
    """
    precision = FEATURES_CONFIG["calculator"]["precision"]
    result = a - b
    return round(result, precision)


def multiply(a: float, b: float) -> float:
    """
    Multiplica dois números.

    Args:
        a: Primeiro número
        b: Segundo número

    Returns:
        Resultado da multiplicação
    """
    precision = FEATURES_CONFIG["calculator"]["precision"]
    result = a * b
    return round(result, precision)


def divide(a: float, b: float) -> float:
    """
    Divide dois números.

    Args:
        a: Dividendo
        b: Divisor

    Returns:
        Resultado da divisão

    Raises:
        ValueError: Se o divisor for zero
    """
    if b == 0:
        raise ValueError("Divisão por zero não é permitida")

    precision = FEATURES_CONFIG["calculator"]["precision"]
    result = a / b
    return round(result, precision)


def power(base: float, exponent: Union[int, float]) -> float:
    """
    Calcula a potência de um número.

    Args:
        base: Base da potência
        exponent: Expoente

    Returns:
        Resultado da potência (base^exponent)
    """
    precision = FEATURES_CONFIG["calculator"]["precision"]
    result = base**exponent
    return round(result, precision)
