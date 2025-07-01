"""
Configurações centralizadas para o servidor FastMCP.
"""
import os
from typing import Dict, Any


class Settings:
    """Configurações do servidor MCP"""
    
    # Configurações do servidor
    SERVER_NAME: str = "Rio de Janeiro MCP Server"
    VERSION: str = "1.0.0"
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"
    
    # Prefixos para recursos
    RESOURCE_PREFIX: str = "rio://resources/"
    
    # Configurações de timezone
    TIMEZONE: str = "America/Sao_Paulo"
    
    # Configurações de logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    
    @classmethod
    def get_server_info(cls) -> Dict[str, Any]:
        """Retorna informações do servidor"""
        return {
            "name": cls.SERVER_NAME,
            "version": cls.VERSION,
            "debug": cls.DEBUG,
            "timezone": cls.TIMEZONE
        }


# Constantes para os recursos
DISTRICTS_DATA = [
    "Copacabana", "Ipanema", "Botafogo", "Flamengo", "Lagoa",
    "Leblon", "Urca", "Leme", "Catete", "Glória", "Santa Teresa",
    "Centro", "Lapa", "Tijuca", "Vila Isabel", "Grajaú", "Andaraí",
    "Maracanã", "Barra da Tijuca", "Recreio dos Bandeirantes",
    "Jacarepaguá", "Freguesia", "Taquara", "Tanque"
]

# Configuração de exemplo para features extras
FEATURES_CONFIG = {
    "calculator": {
        "enabled": True,
        "precision": 2
    },
    "datetime": {
        "enabled": True,
        "formats": ["ISO", "BR", "US"]
    },
    "rio_info": {
        "enabled": True,
        "include_weather": False  # Para futuras expansões
    }
} 