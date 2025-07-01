"""
Utilitários para manipulação de data e hora no timezone do Rio de Janeiro.
"""
from datetime import datetime
from typing import Dict, Any
import pytz
from ..config import Settings


def get_rio_timezone():
    """Retorna o timezone do Rio de Janeiro"""
    return pytz.timezone(Settings.TIMEZONE)


def get_current_rio_time() -> Dict[str, Any]:
    """
    Retorna a data e hora atual no timezone do Rio de Janeiro.
    
    Returns:
        Dict com informações detalhadas da data/hora atual
    """
    rio_tz = get_rio_timezone()
    now = datetime.now(rio_tz)
    
    return {
        "datetime_iso": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "date_br": now.strftime("%d/%m/%Y"),
        "time_12h": now.strftime("%I:%M:%S %p"),
        "weekday": now.strftime("%A"),
        "weekday_pt": _get_weekday_pt(now.weekday()),
        "month": now.strftime("%B"),
        "month_pt": _get_month_pt(now.month),
        "timezone": str(rio_tz),
        "utc_offset": now.strftime("%z")
    }


def _get_weekday_pt(weekday: int) -> str:
    """Converte número do dia da semana para português"""
    weekdays = {
        0: "Segunda-feira",
        1: "Terça-feira", 
        2: "Quarta-feira",
        3: "Quinta-feira",
        4: "Sexta-feira",
        5: "Sábado",
        6: "Domingo"
    }
    return weekdays.get(weekday, "Desconhecido")


def _get_month_pt(month: int) -> str:
    """Converte número do mês para português"""
    months = {
        1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril",
        5: "Maio", 6: "Junho", 7: "Julho", 8: "Agosto",
        9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro"
    }
    return months.get(month, "Desconhecido") 