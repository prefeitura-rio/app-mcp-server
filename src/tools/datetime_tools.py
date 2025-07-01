"""
Ferramentas de data e hora para o servidor FastMCP.
"""
from typing import Dict, Any
from ..utils import get_current_rio_time


def get_current_time() -> Dict[str, Any]:
    """
    Obtém a data e hora atual no timezone do Rio de Janeiro.
    
    Returns:
        Dicionário com informações completas da data/hora atual
    """
    return get_current_rio_time()


def format_greeting() -> str:
    """
    Cria uma saudação personalizada baseada no horário atual.
    
    Returns:
        Mensagem de saudação apropriada para o horário
    """
    time_info = get_current_rio_time()
    current_hour = int(time_info["time"].split(":")[0])
    weekday = time_info["weekday_pt"]
    date_br = time_info["date_br"]
    
    if 5 <= current_hour < 12:
        greeting = "Bom dia"
    elif 12 <= current_hour < 18:
        greeting = "Boa tarde"
    else:
        greeting = "Boa noite"
    
    return f"{greeting}! Hoje é {weekday}, {date_br}. No Rio de Janeiro são {time_info['time']}." 