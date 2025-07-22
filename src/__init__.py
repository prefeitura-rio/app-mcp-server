"""
Servidor FastMCP para o Rio de Janeiro.

Este módulo implementa um servidor MCP (Model Context Protocol) completo
com ferramentas de cálculo, informações sobre o Rio de Janeiro e recursos
de data/hora localizados.
"""

from src.app import app, mcp, create_app
from src.config.settings import Settings
from src.config.env import IS_LOCAL

__version__ = Settings.VERSION

if IS_LOCAL:
    __all__ = ["app", "mcp", "create_app", "Settings"]

else:
    __all__ = ["app", "create_app", "Settings"]
