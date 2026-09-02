"""Testes do bloco de mensagem por dia do Rock in Rio (CHATR-187).

Dois assuntos distintos: a grafia dos nomes, que o site publica de forma
inconsistente, e o layout combinado com o PO, que precisa sair igual toda vez.
"""

from datetime import date

import pytest

from src.tools.rock_in_rio.mensagem import (
    bloco_do_dia,
    bloco_do_palco,
    nome_de_exibicao,
    textos_por_dia,
    textos_por_palco,
)

APP = {
    "android": "https://play.google.com/store/apps/details?id=br.com.rockinrio.app",
    "ios": "https://apps.apple.com/br/app/rock-in-rio/id1478184797",
}
AVISO = "Os horários dos shows só aparecem no aplicativo oficial:"


def _show(data: str, palco: str, artista: str) -> dict:
    return {"data": data, "palco": palco, "artista": artista, "slug": "x"}


@pytest.mark.parametrize(
    "publicado, exibido",
    [
        # O caso comum: o site grita o nome inteiro.
        ("ELTON JOHN", "Elton John"),
        # Conector não é nome e não sobe de caixa.
        (
            "LUÍSA SONZA CONVIDA ROBERTO MENESCAL",
            "Luísa Sonza convida Roberto Menescal",
        ),
        ("PÉRICLES CANTA MOTOWN", "Péricles canta Motown"),
        ("OS GAROTIN CONVIDAM DUQUESA", "Os Garotin convidam Duquesa"),
        ("JOTA QUEST TOCA TIM MAIA", "Jota Quest toca Tim Maia"),
        ("VANESSA DA MATA CONVIDA RUBEL", "Vanessa da Mata convida Rubel"),
        ("ROCK IN GIL COM LARISSA LUZ", "Rock in Gil com Larissa Luz"),
        # Inicial não é palavra gritada.
        ("MC CABELINHO CONVIDA TZ DA CORONEL", "MC Cabelinho convida TZ da Coronel"),
        ("RODRIGO DO CN", "Rodrigo do CN"),
        ("PJ MORTON", "PJ Morton"),
        # Apóstrofo e ponto não iniciam palavra nova; hífen inicia.
        ("MART’NÁLIA", "Mart’nália"),
        ("JOTA.PÊ CONVIDA LUEDJI LUNA", "Jota.pê convida Luedji Luna"),
        ("NE-YO", "Ne-Yo"),
        # Dígito colado no nome é estilização; separado, é parte do nome.
        ("GBZ7N", "GBZ7N"),
        ("MAROON 5", "Maroon 5"),
        # O que o site já grafou passa intacto, inclusive misturado no meio.
        ("mgk", "mgk"),
        ("Wanda Sá", "Wanda Sá"),
        ("ZeROBADASS", "ZeROBADASS"),
        ("AR Baby", "AR Baby"),
        ("ALOK & FAMILY pres. RAVE THE WORLD", "Alok & Family pres. Rave The World"),
        # Exceção conhecida: caixa alta que é a grafia real do artista.
        ("ANNA", "ANNA"),
        ("NEXZ", "NEXZ"),
    ],
)
def test_grafia_do_nome_para_a_mensagem(publicado, exibido):
    assert nome_de_exibicao(publicado) == exibido


def test_bloco_do_dia_sai_no_layout_combinado_com_o_po():
    """Layout literal de propósito.

    Espaçamento e marcador são o pedido do PO, e é isso que o WhatsApp
    transforma em lista. Um teste que só verificasse "contém o artista" deixaria
    o formato mudar sem ninguém perceber.
    """
    shows = [
        _show("2026-09-07", "Palco Mundo", "ELTON JOHN"),
        _show("2026-09-07", "Palco Mundo", "GILBERTO GIL"),
        _show("2026-09-07", "Palco Sunset", "LAUFEY"),
    ]

    bloco = bloco_do_dia(date(2026, 9, 7), shows, "Rock in Rio 2026", AVISO, APP)

    assert bloco == (
        "No dia 7 de setembro do Rock in Rio 2026, as atrações são:\n"
        "\n"
        "- Palco Mundo: Elton John, Gilberto Gil\n"
        "\n"
        "- Palco Sunset: Laufey\n"
        "\n"
        "Os horários dos shows só aparecem no aplicativo oficial:\n"
        "\n"
        f"- iOS: {APP['ios']}\n"
        "\n"
        f"- Android: {APP['android']}"
    )


def test_bloco_preserva_a_ordem_dos_palcos_da_pagina():
    shows = [
        _show("2026-09-07", "Supernova", "ALEE"),
        _show("2026-09-07", "Palco Mundo", "ELTON JOHN"),
        _show("2026-09-07", "Supernova", "MELLY"),
    ]

    bloco = bloco_do_dia(date(2026, 9, 7), shows, "Rock in Rio 2026", AVISO, APP)

    assert bloco.index("Supernova") < bloco.index("Palco Mundo")
    assert "- Supernova: Alee, Melly" in bloco


def test_dia_sem_atracao_nao_vira_bloco():
    """Bloco com cabeçalho e nenhum palco convidaria a dizer que não há show."""
    shows = [_show("2026-09-04", "Palco Mundo", "FOO FIGHTERS")]
    datas = (date(2026, 9, 4), date(2026, 9, 5))

    textos = textos_por_dia(shows, datas, "Rock in Rio 2026", AVISO, APP)

    assert list(textos) == ["2026-09-04"]


def test_bloco_do_palco_lista_um_item_por_dia():
    """Mesmo layout do bloco de dia, com o eixo trocado.

    O cabeçalho não leva preposição colada ao nome do palco: "no Supernova" e
    "na Supernova" soam ambos errados, e é o palco que varia aqui.
    """
    shows = [
        _show("2026-09-04", "Palco Sunset", "HOT MILK"),
        _show("2026-09-06", "Palco Sunset", "NE-YO"),
        _show("2026-09-06", "Palco Sunset", "CALEMA"),
    ]
    datas = (date(2026, 9, 4), date(2026, 9, 5), date(2026, 9, 6))

    bloco = bloco_do_palco("Palco Sunset", shows, datas, "Rock in Rio 2026", AVISO, APP)

    assert bloco == (
        "As atrações do Palco Sunset no Rock in Rio 2026 são:\n"
        "\n"
        "- 04/09 (sexta-feira): Hot Milk\n"
        "\n"
        "- 06/09 (domingo): Ne-Yo, Calema\n"
        "\n"
        "Os horários dos shows só aparecem no aplicativo oficial:\n"
        "\n"
        f"- iOS: {APP['ios']}\n"
        "\n"
        f"- Android: {APP['android']}"
    )


def test_bloco_do_palco_ignora_dia_sem_atracao_naquele_palco():
    shows = [_show("2026-09-04", "Supernova", "ALEE")]
    datas = (date(2026, 9, 4), date(2026, 9, 5))

    bloco = bloco_do_palco("Supernova", shows, datas, "Rock in Rio 2026", AVISO, APP)

    assert "05/09" not in bloco


def test_textos_por_palco_segue_a_ordem_dos_palcos_na_pagina():
    shows = [
        _show("2026-09-04", "Supernova", "ALEE"),
        _show("2026-09-04", "Palco Mundo", "FOO FIGHTERS"),
    ]

    textos = textos_por_palco(
        shows, (date(2026, 9, 4),), "Rock in Rio 2026", AVISO, APP
    )

    assert list(textos) == ["Supernova", "Palco Mundo"]
