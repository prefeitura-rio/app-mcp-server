"""
Ponto de entrada principal para o servidor FastMCP do Rio de Janeiro.
"""
import sys
from pathlib import Path

# Adiciona o diretório src ao Python path
sys.path.insert(0, str(Path(__file__).parent))

from app import app


def main():
    """Função principal - executa o servidor FastMCP"""
    app.run()


if __name__ == "__main__":
    main()
