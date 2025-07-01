"""
Ponto de entrada principal para o servidor FastMCP do Rio de Janeiro.
"""
import sys
from pathlib import Path

# Adiciona o diretório src ao Python path
sys.path.insert(0, str(Path(__file__).parent))

from app import mcp


def main():
    """Executa o servidor FastMCP usando transporte HTTP (Streamable)."""
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=80,
        path="/mcp",
        message_path="/mcp/messages",
    )


if __name__ == "__main__":
    main()
