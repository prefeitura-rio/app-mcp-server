"""
Servidor FastMCP para o Rio de Janeiro.

Este módulo implementa um servidor MCP (Model Context Protocol) completo
com ferramentas de cálculo, informações sobre o Rio de Janeiro e recursos
de data/hora localizados.

`app`, `mcp` e `create_app` são resolvidos sob demanda (PEP 562). Importá-los
de forma ansiosa fazia com que qualquer `import src.<algo>` construísse a
aplicação inteira e importasse `src.config.env` — que aborta na primeira
variável de ambiente faltante. Como `python -m src.main` importa o pacote
`src` antes de executar `main.py`, isso acontecia antes do preflight, que
existe justamente para reportar todas as variáveis faltantes de uma vez.
"""

from src.config.settings import Settings

__version__ = Settings.VERSION

__all__ = ["app", "mcp", "create_app", "Settings"]

_LAZY_ATTRS = frozenset({"app", "mcp", "create_app"})


def __getattr__(name: str):
    """Carrega a aplicação apenas quando um de seus símbolos é acessado."""
    if name in _LAZY_ATTRS:
        import src.app

        return getattr(src.app, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted([*__all__, "__version__"])
