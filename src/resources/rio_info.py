"""
Recursos de informações sobre o Rio de Janeiro para o servidor FastMCP.
"""
from typing import List, Dict, Any
from ..config import DISTRICTS_DATA, Settings


def get_districts_list() -> List[str]:
    """
    Retorna a lista de bairros do Rio de Janeiro.
    
    Returns:
        Lista com nomes dos principais bairros do Rio de Janeiro
    """
    return DISTRICTS_DATA.copy()


def get_rio_basic_info() -> Dict[str, Any]:
    """
    Retorna informações básicas sobre o Rio de Janeiro.
    
    Returns:
        Dicionário com informações básicas da cidade
    """
    return {
        "nome": "Rio de Janeiro",
        "estado": "Rio de Janeiro",
        "regiao": "Sudeste",
        "populacao_aproximada": 6_747_815,  # Região metropolitana
        "area_km2": 1_200.27,
        "temperatura_media": "23°C",
        "pontos_turisticos": [
            "Cristo Redentor",
            "Pão de Açúcar", 
            "Copacabana",
            "Ipanema",
            "Maracanã",
            "Lapa",
            "Santa Teresa"
        ],
        "principais_praias": [
            "Copacabana",
            "Ipanema", 
            "Leblon",
            "Barra da Tijuca",
            "Recreio dos Bandeirantes"
        ],
        "codigo_area": "21",
        "fuso_horario": Settings.TIMEZONE,
        "fundacao": "1565",
        "alcunhas": [
            "Cidade Maravilhosa",
            "Capital do Samba",
            "Cidade do Rock"
        ]
    }


def get_greeting_message() -> str:
    """
    Retorna uma mensagem de boas-vindas personalizada.
    
    Returns:
        Mensagem de boas-vindas para o servidor MCP do Rio
    """
    return (
        "Bem-vindo ao servidor MCP da Cidade Maravilhosa! "
        "Aqui você pode acessar informações sobre o Rio de Janeiro, "
        "usar ferramentas de cálculo e obter dados atualizados sobre a cidade."
    ) 