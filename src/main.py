"""
Ponto de entrada principal para o servidor FastMCP do Rio de Janeiro.
"""

import os
import sys
import argparse
from pathlib import Path

# Adiciona o diretório raiz do projeto ao Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.app import mcp
from src.config import env

if __name__ == "__main__":

    if env.IS_LOCAL:
        mcp.run()
    else:
        mcp.run(transport="streamable-http", host="0.0.0.0", port=80, path="/mcp")
