"""Testes da tool de line-up do Rock in Rio (CHATR-187).

O grosso daqui é semântica de data. "Hoje" é ambíguo neste evento por dois
motivos que se somam: a programação de um dia avança pela madrugada do dia
seguinte, e o festival tem um intervalo (08 a 10 de setembro) em que ele não
terminou, mas também não há show. Cada um desses casos tem um teste próprio.
"""

from datetime import datetime

import pytest

from src.tools.rock_in_rio import tool as tool_mod
from src.tools.rock_in_rio.cache import LineupCarregado, LineupIndisponivel
from src.tools.rock_in_rio.tool import (
    APP_OFICIAL,
    _situacao_temporal,
    get_rock_in_rio_lineup,
)
from src.utils.datetime_utils import get_rio_timezone

SHOWS = [
    {
        "data": "2026-09-04",
        "dia_slug": "04-set",
        "palco": "Palco Mundo",
        "artista": "FOO FIGHTERS",
        "slug": "foo-fighters",
        "url": "https://rockinrio.com/rio/pt-br/line-up/foo-fighters/",
    },
    {
        "data": "2026-09-13",
        "dia_slug": "13-set",
        "palco": "Palco Sunset",
        "artista": "IVETE SANGALO",
        "slug": "ivete-sangalo",
        "url": "https://rockinrio.com/rio/pt-br/line-up/ivete-sangalo/",
    },
]


def _momento(ano, mes, dia, hora, minuto=0) -> datetime:
    return get_rio_timezone().localize(datetime(ano, mes, dia, hora, minuto))


def test_antes_do_festival_aponta_o_primeiro_dia():
    situacao = _situacao_temporal(_momento(2026, 8, 28, 14))

    assert situacao["status"] == "antes_do_festival"
    assert situacao["hoje_tem_show"] is False
    assert situacao["proximo_dia_com_show"]["data"] == "2026-09-04"
    assert situacao["data_de_hoje"] == "2026-08-28"


def test_dia_de_show_a_noite():
    situacao = _situacao_temporal(_momento(2026, 9, 4, 20))

    assert situacao["status"] == "durante_o_festival"
    assert situacao["hoje_tem_show"] is True
    assert situacao["jornada_de_referencia"] == "2026-09-04"
    assert situacao["proximo_dia_com_show"]["data"] == "2026-09-05"
    assert "observacao_jornada" not in situacao


def test_madrugada_ainda_pertence_a_programacao_da_vespera():
    """Às 2h de sábado, o que está rolando é a programação de sexta.

    Sem esse deslocamento, quem pergunta de dentro da Cidade do Rock de
    madrugada — o público mais provável naquele horário — receberia a resposta
    errada.
    """
    situacao = _situacao_temporal(_momento(2026, 9, 5, 2))

    assert situacao["data_de_hoje"] == "2026-09-05"
    assert situacao["jornada_de_referencia"] == "2026-09-04"
    assert situacao["hoje_tem_show"] is True
    assert "observacao_jornada" in situacao
    assert "04/09/2026" in situacao["observacao_jornada"]


def test_intervalo_no_meio_do_festival():
    """09 de setembro: o festival não terminou, mas não há show."""
    situacao = _situacao_temporal(_momento(2026, 9, 9, 15))

    assert situacao["status"] == "durante_o_festival"
    assert situacao["hoje_tem_show"] is False
    assert situacao["proximo_dia_com_show"]["data"] == "2026-09-11"
    assert "intervalo" in situacao["observacao"]


def test_madrugada_do_ultimo_dia_ainda_e_festival():
    """05h de 14/09 ainda é a madrugada do dia 13."""
    situacao = _situacao_temporal(_momento(2026, 9, 14, 5))

    assert situacao["status"] == "durante_o_festival"
    assert situacao["jornada_de_referencia"] == "2026-09-13"
    assert situacao["hoje_tem_show"] is True
    assert situacao["proximo_dia_com_show"] is None


def test_depois_do_festival_responde_encerrado():
    situacao = _situacao_temporal(_momento(2026, 9, 14, 7))

    assert situacao["status"] == "encerrado"
    assert situacao["hoje_tem_show"] is False
    assert situacao["proximo_dia_com_show"] is None
    assert "encerrada" in situacao["observacao"]


def test_muito_depois_do_festival_continua_encerrado():
    situacao = _situacao_temporal(_momento(2027, 1, 10, 12))

    assert situacao["status"] == "encerrado"
    assert situacao["proximo_dia_com_show"] is None


@pytest.fixture
def lineup_ok(monkeypatch):
    async def falso_obter_lineup(**_):
        return LineupCarregado(SHOWS, __import__("time").time() - 120, "memoria")

    monkeypatch.setattr(tool_mod, "obter_lineup", falso_obter_lineup)


@pytest.mark.asyncio
async def test_resposta_traz_a_grade_e_os_links_do_app(lineup_ok):
    resposta = await get_rock_in_rio_lineup()

    assert resposta["disponivel"] is True
    assert resposta["total_de_atracoes"] == 2
    assert resposta["shows"] == SHOWS
    assert resposta["app_oficial"] == APP_OFICIAL
    assert "play.google.com" in resposta["app_oficial"]["android"]
    assert "apps.apple.com" in resposta["app_oficial"]["ios"]


@pytest.mark.asyncio
async def test_resposta_declara_que_nao_ha_horarios(lineup_ok):
    """O risco número um desta tool é o modelo inventar horário de show."""
    resposta = await get_rock_in_rio_lineup()

    assert resposta["horarios"]["disponiveis"] is False
    assert "aplicativo oficial" in resposta["horarios"]["onde_consultar"].lower()
    assert "NÃO informe" in resposta["horarios"]["aviso"]
    assert "horário" in resposta["instrucoes_de_resposta"]

    # Nenhuma atração pode carregar campo de hora: não existe na fonte.
    for show in resposta["shows"]:
        assert not any("hor" in chave.lower() for chave in show)


@pytest.mark.asyncio
async def test_resposta_lista_os_sete_dias_e_os_palcos(lineup_ok):
    resposta = await get_rock_in_rio_lineup()

    dias = resposta["evento"]["dias"]
    assert len(dias) == 7
    assert dias[0]["data"] == "2026-09-04"
    assert dias[0]["data_br"] == "04/09/2026"
    assert dias[-1]["data"] == "2026-09-13"
    assert resposta["evento"]["palcos"] == ["Palco Mundo", "Palco Sunset"]


@pytest.mark.asyncio
async def test_resposta_informa_a_procedencia_do_dado(lineup_ok):
    resposta = await get_rock_in_rio_lineup()

    assert resposta["atualizado_em"]["origem"] == "memoria"
    assert 100 <= resposta["atualizado_em"]["ha_segundos"] <= 200


@pytest.mark.asyncio
async def test_indisponibilidade_nao_entrega_grade_alguma(monkeypatch):
    """Preferimos não entregar a entregar errado.

    A resposta de falha não pode conter nada que o modelo consiga apresentar
    como programação — só o aviso e o app oficial.
    """

    async def explode(**_):
        raise LineupIndisponivel("fonte fora do ar e cache vencido")

    monkeypatch.setattr(tool_mod, "obter_lineup", explode)

    resposta = await get_rock_in_rio_lineup()

    assert resposta["disponivel"] is False
    assert "shows" not in resposta
    assert "NÃO informe line-up" in resposta["instrucoes_de_resposta"]
    assert resposta["app_oficial"] == APP_OFICIAL


@pytest.mark.asyncio
async def test_erro_inesperado_tambem_degrada_com_seguranca(monkeypatch):
    async def explode(**_):
        raise ValueError("algo que ninguém previu")

    monkeypatch.setattr(tool_mod, "obter_lineup", explode)

    resposta = await get_rock_in_rio_lineup()

    assert resposta["disponivel"] is False
    assert "shows" not in resposta
    assert resposta["app_oficial"] == APP_OFICIAL


def test_descricao_da_tool_avisa_que_nao_tem_horario():
    """O modelo precisa saber o que a tool não entrega antes de chamá-la."""
    descricao = tool_mod._descricao_da_tool("vTESTE")

    assert "vTESTE" in descricao
    assert "NÃO devolve horários" in descricao
    assert "aplicativo oficial" in descricao
