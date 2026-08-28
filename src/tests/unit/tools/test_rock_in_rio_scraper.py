"""Testes do parser do line-up do Rock in Rio (CHATR-187).

As fixtures `dia-04-set.html` e `dia-11-set.html` são páginas reais do site
oficial, salvas na íntegra. É de propósito: o valor destes testes está em
detectar que o tema do site mudou, e um HTML sintético reduzido não detectaria.
O dia 11 entra junto porque tem uma atração a mais que o dia 04, num palco
diferente — cobre o caso em que os palcos não têm todos o mesmo tamanho.
"""

from datetime import date
from pathlib import Path

import pytest

from src.tools.rock_in_rio import scraper as scraper_mod
from src.tools.rock_in_rio.scraper import (
    DIAS_DO_EVENTO,
    LineupInvalido,
    parse_dia,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "rock_in_rio"

PALCOS_ESPERADOS = [
    "Palco Mundo",
    "Palco Sunset",
    "New Dance Order",
    "Espaço Favela",
    "Global Village",
    "Supernova",
]


def _ler(nome: str) -> str:
    return (FIXTURES / nome).read_text(encoding="utf-8")


def test_parse_dia_04_extrai_a_grade_completa():
    shows = parse_dia(_ler("dia-04-set.html"), dia_slug="04-set", data=date(2026, 9, 4))

    assert len(shows) == 22
    assert [s.palco for s in shows][:1] == ["Palco Mundo"]
    assert shows[0].artista == "FOO FIGHTERS"
    assert shows[0].slug == "foo-fighters"
    assert shows[0].data == "2026-09-04"
    assert shows[0].dia_slug == "04-set"
    assert shows[0].url.endswith("/line-up/foo-fighters/")


def test_parse_dia_04_agrupa_por_palco_na_ordem_do_documento():
    shows = parse_dia(_ler("dia-04-set.html"), dia_slug="04-set", data=date(2026, 9, 4))

    # A ordem de aparição dos palcos precisa ser preservada: é ela que carrega
    # o vínculo artista → palco neste HTML.
    vistos = []
    for show in shows:
        if show.palco not in vistos:
            vistos.append(show.palco)

    assert vistos == PALCOS_ESPERADOS


def test_parse_dia_11_tem_uma_atracao_a_mais_no_new_dance_order():
    shows = parse_dia(
        _ler("dia-11-set.html"), dia_slug="11-set", data=date(2026, 9, 11)
    )

    assert len(shows) == 23
    assert sum(1 for s in shows if s.palco == "New Dance Order") == 5
    assert shows[0].artista == "STRAY KIDS"


def test_parse_ignora_os_links_de_filtro_por_palco():
    """Os filtros do topo da página apontam para `/line-up/palco/<slug>/`.

    Eles casariam com o padrão de artista se a varredura não estivesse limitada
    ao bloco de resultados — e entrariam na grade como atrações fantasma.
    """
    shows = parse_dia(_ler("dia-04-set.html"), dia_slug="04-set", data=date(2026, 9, 4))

    assert all(not s.slug.startswith("palco") for s in shows)
    assert all("/line-up/palco/" not in s.url for s in shows)


def _pagina(corpo: str) -> str:
    return f'<html><body><section class="resultado">{corpo}</section></body></html>'


def _artista(slug: str, nome: str) -> str:
    return (
        f'<div class="bloco-artista"><div class="item">'
        f'<a href="https://rockinrio.com/rio/pt-br/line-up/{slug}/">'
        f'<h2 class="dest-1">{nome}<i class="fas"></i></h2></a></div></div>'
    )


def _palco(nome: str) -> str:
    return f'<div class="data"><span>{nome}</span></div>'


def test_remove_caracteres_invisiveis_do_nome():
    """O CMS publica "AVENGED SEVENFOLD" com zero-width space no fim.

    Escrito como escape para o caractere ficar visível em revisão.
    """
    html = _pagina(
        _palco("Palco Mundo") + _artista("avenged", "AVENGED SEVENFOLD\u200b")
    )

    shows = parse_dia(html, dia_slug="05-set", data=date(2026, 9, 5))

    assert shows[0].artista == "AVENGED SEVENFOLD"


def test_desescapa_entidades_html_do_nome():
    html = _pagina(_palco("Palco Sunset") + _artista("mumford", "MUMFORD &amp; SONS"))

    shows = parse_dia(html, dia_slug="12-set", data=date(2026, 9, 12))

    assert shows[0].artista == "MUMFORD & SONS"


def test_normaliza_espacos_em_excesso():
    html = _pagina(_palco("Palco Mundo") + _artista("x", "  FOO\n\n  FIGHTERS  "))

    shows = parse_dia(html, dia_slug="04-set", data=date(2026, 9, 4))

    assert shows[0].artista == "FOO FIGHTERS"


def test_pagina_sem_bloco_de_resultados_levanta():
    with pytest.raises(LineupInvalido, match="não encontrado"):
        parse_dia(
            "<html><body>nada aqui</body></html>",
            dia_slug="04-set",
            data=date(2026, 9, 4),
        )


def test_pagina_sem_atracoes_levanta():
    """Zero atrações precisa ser erro, não lista vazia.

    Devolver vazio faria o chatbot afirmar que uma banda não toca no festival.
    """
    with pytest.raises(LineupInvalido, match="Nenhuma atração"):
        parse_dia(
            _pagina(_palco("Palco Mundo")), dia_slug="04-set", data=date(2026, 9, 4)
        )


def test_artista_antes_de_qualquer_palco_levanta():
    """Se o agrupamento por ordem no documento cair, o parser precisa gritar."""
    html = _pagina(_artista("orfao", "BANDA SEM PALCO") + _palco("Palco Mundo"))

    with pytest.raises(LineupInvalido, match="antes de qualquer palco"):
        parse_dia(html, dia_slug="04-set", data=date(2026, 9, 4))


def test_dias_do_evento_cobre_as_sete_datas():
    assert len(DIAS_DO_EVENTO) == 7
    assert [slug for slug, _ in DIAS_DO_EVENTO] == [
        "04-set",
        "05-set",
        "06-set",
        "07-set",
        "11-set",
        "12-set",
        "13-set",
    ]
    # O intervalo de 08 a 10 de setembro não é engano de digitação: o festival
    # realmente pausa no meio, e `tool.py` depende disso.
    assert [data.day for _, data in DIAS_DO_EVENTO] == [4, 5, 6, 7, 11, 12, 13]
    assert all(data.year == 2026 and data.month == 9 for _, data in DIAS_DO_EVENTO)


@pytest.mark.asyncio
async def test_buscar_lineup_junta_os_sete_dias(monkeypatch):
    paginas = {
        slug: _pagina(
            _palco("Palco Mundo") + _artista(f"banda-{slug}", f"BANDA {slug}")
        )
        for slug, _ in DIAS_DO_EVENTO
    }

    async def baixar(_client, dia_slug):
        return paginas[dia_slug]

    monkeypatch.setattr(scraper_mod, "_baixar_dia", baixar)

    shows = await scraper_mod.buscar_lineup()

    assert len(shows) == 7
    assert {s.dia_slug for s in shows} == {slug for slug, _ in DIAS_DO_EVENTO}


@pytest.mark.asyncio
async def test_buscar_lineup_e_tudo_ou_nada(monkeypatch):
    """Um dia que não baixa invalida a busca inteira.

    Grade parcial é o pior desfecho possível: o chatbot afirmaria com convicção
    que uma banda não está no festival só porque a página dela não respondeu.
    """

    async def baixar(_client, dia_slug):
        if dia_slug == "11-set":
            raise ConnectionError("timeout")
        return _pagina(_palco("Palco Mundo") + _artista("banda", "BANDA"))

    monkeypatch.setattr(scraper_mod, "_baixar_dia", baixar)

    with pytest.raises(ConnectionError):
        await scraper_mod.buscar_lineup()


@pytest.mark.asyncio
async def test_buscar_lineup_propaga_pagina_fora_do_formato(monkeypatch):
    async def baixar(_client, dia_slug):
        if dia_slug == "06-set":
            return "<html><body>o tema do site mudou</body></html>"
        return _pagina(_palco("Palco Mundo") + _artista("banda", "BANDA"))

    monkeypatch.setattr(scraper_mod, "_baixar_dia", baixar)

    with pytest.raises(LineupInvalido):
        await scraper_mod.buscar_lineup()
