"""
Ferramentas de data e hora para o servidor FastMCP.
"""

import json

from src.utils.datetime_utils import get_current_rio_time


def get_current_time() -> str:
    """
    Obtém a data e hora atual no timezone do Rio de Janeiro.

    Returns:
        Dicionário com informações completas da data/hora atual
    """

    current_rio_time = get_current_rio_time()
    return json.dumps(current_rio_time, ensure_ascii=False, indent=2)
