import json
import sys

import pytest


calculator = sys.modules["src.tools.calculator"]
datetime_tools = sys.modules["src.tools.datetime_tools"]
datetime_utils = sys.modules["src.utils.datetime_utils"]
rio_info = sys.modules["src.resources.rio_info"]
settings = sys.modules["src.config.settings"]


def test_calculator_operations_respect_precision():
    assert calculator.add(1.111, 2.225) == 3.34
    assert calculator.subtract(10.123, 2.119) == 8.0
    assert calculator.multiply(2.345, 3) == 7.04
    assert calculator.divide(10, 4) == 2.5
    assert calculator.power(2, 3) == 8


def test_calculator_divide_by_zero_raises():
    with pytest.raises(ValueError, match="Divisão por zero"):
        calculator.divide(1, 0)


def test_datetime_tools_get_current_time_serializes_payload(monkeypatch):
    fake_time = {
        "datetime_iso": "2026-04-08T09:30:00-03:00",
        "date": "2026-04-08",
        "time": "09:30:00",
        "date_br": "08/04/2026",
        "time_12h": "09:30:00 AM",
        "weekday": "Wednesday",
        "weekday_pt": "Quarta-feira",
        "month": "April",
        "month_pt": "Abril",
        "timezone": "America/Sao_Paulo",
        "utc_offset": "-0300",
    }
    monkeypatch.setattr(datetime_tools, "get_current_rio_time", lambda: fake_time)

    payload = json.loads(datetime_tools.get_current_time())

    assert payload == fake_time


@pytest.mark.parametrize(
    ("hour", "expected"),
    [("09:30:00", "Bom dia"), ("15:30:00", "Boa tarde"), ("21:30:00", "Boa noite")],
)
def test_datetime_tools_format_greeting_by_hour(monkeypatch, hour, expected):
    monkeypatch.setattr(datetime_tools, "get_current_rio_time", lambda: {"time": hour})

    result = datetime_tools.format_greeting()

    assert expected in result
    assert "canal seguro e oficial" in result


def test_datetime_utils_translate_unknown_values():
    assert datetime_utils._get_weekday_pt(99) == "Desconhecido"
    assert datetime_utils._get_month_pt(99) == "Desconhecido"


def test_settings_get_server_info_returns_expected_shape():
    result = settings.Settings.get_server_info()

    assert result["name"] == settings.Settings.SERVER_NAME
    assert result["version"] == settings.Settings.VERSION
    assert result["timezone"] == settings.Settings.TIMEZONE
    assert isinstance(result["debug"], bool)


def test_rio_info_returns_copy_of_districts():
    districts = rio_info.get_districts_list()
    districts.append("Bairro Inventado")

    assert "Bairro Inventado" not in rio_info.get_districts_list()


def test_rio_basic_info_and_greeting_message():
    info = rio_info.get_rio_basic_info()

    assert info["nome"] == "Rio de Janeiro"
    assert info["fuso_horario"] == settings.Settings.TIMEZONE
    assert "Copacabana" in info["principais_praias"]
    assert "Cidade Maravilhosa" in info["alcunhas"]
    assert "Bem-vindo ao servidor MCP" in rio_info.get_greeting_message()
