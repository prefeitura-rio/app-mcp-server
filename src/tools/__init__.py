"""
Módulo de ferramentas (tools) para o servidor FastMCP.
"""

from .calculator import add, subtract, multiply, divide, power
from .datetime_tools import get_current_time, format_greeting
from .equipamentos import get_equipaments_categories, get_equipaments, get_google_search

__all__ = [
    "add",
    "subtract",
    "multiply",
    "divide",
    "power",
    "get_current_time",
    "format_greeting",
    "get_equipaments_categories",
    "get_equipaments",
    "get_google_search",
]
