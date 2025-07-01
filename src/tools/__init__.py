"""
Módulo de ferramentas (tools) para o servidor FastMCP.
"""

from .calculator import add, subtract, multiply, divide, power
from .datetime_tools import get_current_time, format_greeting

__all__ = [
    "add", "subtract", "multiply", "divide", "power",
    "get_current_time", "format_greeting"
]
