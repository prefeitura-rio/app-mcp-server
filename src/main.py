"""
Ponto de entrada principal para o servidor FastMCP do Rio de Janeiro.
"""
import sys
from pathlib import Path
import uvicorn
from app import http_app

# Adiciona o diretório src ao Python path
sys.path.insert(0, str(Path(__file__).parent))


def main():
    """Executa o servidor ASGI via Uvicorn (1 worker)."""
    uvicorn.run(
        "app:http_app",
        host="0.0.0.0",
        port=80,
        log_level="info",
        workers=1,
        factory=False,
    )


if __name__ == "__main__":
    main()
