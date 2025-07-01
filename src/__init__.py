"""
Servidor FastMCP para o Rio de Janeiro.

Este módulo implementa um servidor MCP (Model Context Protocol) completo
com ferramentas de cálculo, informações sobre o Rio de Janeiro e recursos
de data/hora localizados.
"""

from .app import app, create_app
from .config import Settings

__version__ = Settings.VERSION
__all__ = ["app", "create_app", "Settings"]
