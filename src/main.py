"""
Ponto de entrada principal para o servidor FastMCP do Rio de Janeiro.
"""

import os
import sys
from pathlib import Path

# Adiciona o diretório raiz do projeto ao Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.app import mcp
from src.config import env

if __name__ == "__main__":
    if env.IS_LOCAL:
        mcp.run()
    else:
        port = int(os.environ.get("PORT", 80))
        mcp.run(transport="streamable-http", host="0.0.0.0", port=port, path="/mcp")
