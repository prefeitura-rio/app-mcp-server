"""Testes do parser do line-up do Rock in Rio (CHATR-187).

As fixtures são as páginas reais dos sete dias do site oficial, salvas na
íntegra. É de propósito: o valor destes testes está em detectar que o tema do
site mudou, e um HTML sintético reduzido não detectaria. Salvar os sete, e não
uma amostra, é o que dá cobertura offline à premissa de que todos os dias têm a
mesma estrutura — foi assim que apareceu o `<span>` de nota de rodapé no nome
da MEDUZA, presente só no dia 06.
"""

from datetime import date
from pathlib import Path

import pytest

from src.tools.rock_in_rio import scraper as scraper_mod
from src.tools.rock_in_rio.scraper import (
    DIAS_DO_EVENTO,
    _INICIO_RESULTADO,
    MAX_TAMANHO_NOME,
    MIN_ATRACOES_POR_DIA,
    LineupInvalido,
    parse_dia,
    url_do_artista,
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


def _enchimento(prefixo: str = "banda") -> str:
    """Atrações de preenchimento até o dia passar do piso de sanidade.

    `parse_dia` derruba o dia que vier com menos de `MIN_ATRACOES_POR_DIA`
    atrações, então o HTML sintético precisa de volume mesmo quando o que o
    teste observa é uma atração só — que continua sendo a primeira da lista.
    """
    return "".join(
        _artista(f"{prefixo}-{i}", f"BANDA {i}") for i in range(MIN_ATRACOES_POR_DIA)
    )


def test_remove_caracteres_invisiveis_do_nome():
    """O CMS publica "AVENGED SEVENFOLD" com zero-width space no fim.

    Escrito como escape para o caractere ficar visível em revisão.
    """
    html = _pagina(
        _palco("Palco Mundo")
        + _artista("avenged", "AVENGED SEVENFOLD\u200b")
        + _enchimento()
    )

    shows = parse_dia(html, dia_slug="05-set", data=date(2026, 9, 5))

    assert shows[0].artista == "AVENGED SEVENFOLD"


def test_desescapa_entidades_html_do_nome():
    html = _pagina(
        _palco("Palco Sunset")
        + _artista("mumford", "MUMFORD &amp; SONS")
        + _enchimento()
    )

    shows = parse_dia(html, dia_slug="12-set", data=date(2026, 9, 12))

    assert shows[0].artista == "MUMFORD & SONS"


def test_normaliza_espacos_em_excesso():
    html = _pagina(
        _palco("Palco Mundo") + _artista("x", "  FOO\n\n  FIGHTERS  ") + _enchimento()
    )

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


_ANCORA_ARTISTA = '<a href="https://rockinrio.com/rio/pt-br/line-up/'


def test_section_no_meio_do_bloco_nao_trunca_o_dia():
    """Um `<section>` no meio da lista não pode cortar o dia pela metade.

    Enquanto a varredura parava no primeiro `</section>`, bastava o tema passar
    a inserir um banner entre as atrações para o dia sair truncado — e sem erro
    nenhum, que é justamente o desfecho que o desenho inteiro tenta evitar.
    """
    html = _ler("dia-04-set.html")
    inicio = html.index(_INICIO_RESULTADO)
    segunda_atracao = html.index(
        _ANCORA_ARTISTA, html.index(_ANCORA_ARTISTA, inicio) + 1
    )
    com_banner = (
        html[:segunda_atracao]
        + '<section class="banner"><p>publicidade</p></section>'
        + html[segunda_atracao:]
    )

    shows = parse_dia(com_banner, dia_slug="04-set", data=date(2026, 9, 4))

    assert len(shows) == 22


def test_icone_ausente_nao_derruba_a_atracao():
    """O `<i>` é decoração e não pode ser âncora de nada.

    Enquanto o nome fechava nele, um tema que parasse de usá-lo fazia a atração
    sumir da grade. Ancorado no `</h2>`, que é estrutural, o bloco continua
    legível sem o ícone.
    """
    html = _ler("dia-04-set.html")
    inicio = html.index(_INICIO_RESULTADO)
    sem_icone = html[:inicio] + html[inicio:].replace("<i ", "<span ", 1)

    shows = parse_dia(sem_icone, dia_slug="04-set", data=date(2026, 9, 4))

    assert len(shows) == 22
    assert shows[0].artista == "FOO FIGHTERS"


def test_bloco_de_artista_malformado_levanta():
    """Um `<h2>` sem fechamento faz o bloco não casar — e isso precisa gritar.

    Sem a conferência de âncoras, o dia sairia com uma atração a menos e o
    chatbot negaria uma banda que está no festival.
    """
    html = _ler("dia-04-set.html")
    inicio = html.index(_INICIO_RESULTADO)
    quebrado = html[:inicio] + html[inicio:].replace("</h2>", "</h3>", 1)

    with pytest.raises(LineupInvalido, match="formato do bloco mudou"):
        parse_dia(quebrado, dia_slug="04-set", data=date(2026, 9, 4))


def test_marcador_de_nota_de_rodape_nao_entra_no_nome():
    """O site publica `MEDUZA<span class="fonte-superscript-3">³</span>`.

    O expoente é nota de rodapé do site, não parte do nome — e o NFKC de
    `_limpar_texto` o converteria em dígito, entregando "MEDUZA3" ao cidadão.
    """
    shows = parse_dia(_ler("dia-06-set.html"), dia_slug="06-set", data=date(2026, 9, 6))

    meduza = [s for s in shows if s.slug == "meduza"]
    assert [s.artista for s in meduza] == ["MEDUZA"]


def test_as_sete_paginas_reais_parseiam_com_a_mesma_estrutura():
    """A premissa do desenho é que os sete dias têm a mesma forma.

    O total de 156 atrações e os seis palcos são os números levantados na
    investigação da fonte (CHATR-187); divergir deles é sinal de que o site
    mudou ou de que uma fixture ficou desatualizada.
    """
    total = 0
    palcos = set()
    for slug, data in DIAS_DO_EVENTO:
        shows = parse_dia(_ler(f"dia-{slug}.html"), dia_slug=slug, data=data)
        assert len(shows) >= MIN_ATRACOES_POR_DIA, slug
        assert all(s.artista and s.palco for s in shows), slug
        total += len(shows)
        palcos |= {s.palco for s in shows}

    assert total == 156
    assert palcos == set(PALCOS_ESPERADOS)


def test_dia_abaixo_do_piso_de_atracoes_levanta():
    """Meia grade é pior que grade nenhuma: o chatbot negaria bandas reais."""
    html = _pagina(
        _palco("Palco Mundo")
        + "".join(
            _artista(f"banda-{i}", f"BANDA {i}")
            for i in range(MIN_ATRACOES_POR_DIA - 1)
        )
    )

    with pytest.raises(LineupInvalido, match="abaixo do piso"):
        parse_dia(html, dia_slug="04-set", data=date(2026, 9, 4))


def test_nome_de_artista_acima_do_teto_levanta():
    """Nome vindo do CMS de terceiro entra no contexto do modelo; tem teto."""
    html = _pagina(
        _palco("Palco Mundo")
        + _artista("gigante", "X" * (MAX_TAMANHO_NOME + 1))
        + _enchimento()
    )

    with pytest.raises(LineupInvalido, match="acima do teto"):
        parse_dia(html, dia_slug="04-set", data=date(2026, 9, 4))


def test_nome_de_palco_acima_do_teto_levanta():
    html = _pagina(_palco("P" * (MAX_TAMANHO_NOME + 1)) + _enchimento())

    with pytest.raises(LineupInvalido, match="acima do teto"):
        parse_dia(html, dia_slug="04-set", data=date(2026, 9, 4))


def test_url_do_artista_reproduz_a_url_publicada_em_todos_os_dias():
    """A prova de que `url` pode sair da resposta sem perda.

    Se um dia o site publicar a página do artista em outro caminho, é aqui que
    isso aparece — e aí `Show.para_resposta` precisa voltar a levar o campo.
    """
    for slug, data in DIAS_DO_EVENTO:
        for show in parse_dia(_ler(f"dia-{slug}.html"), dia_slug=slug, data=data):
            assert url_do_artista(show.slug) == show.url


def test_parse_dia_normaliza_link_relativo_do_artista():
    html = _pagina(
        _palco("Palco Mundo")
        + _artista("foo-fighters", "FOO FIGHTERS").replace("https://rockinrio.com", "")
        + _enchimento()
    )

    shows = parse_dia(html, dia_slug="04-set", data=date(2026, 9, 4))

    assert shows[0].url == "https://rockinrio.com/rio/pt-br/line-up/foo-fighters/"


def test_para_resposta_deixa_de_fora_os_campos_derivaveis():
    """O `Show` guarda tudo; a resposta leva só o que não dá para derivar."""
    shows = parse_dia(_ler("dia-04-set.html"), dia_slug="04-set", data=date(2026, 9, 4))

    resposta = shows[0].para_resposta()

    assert set(resposta) == {"data", "palco", "artista", "slug"}
    assert resposta["slug"] == "foo-fighters"
    # O `Show` continua com os dois: o runner de contrato agrupa por `dia_slug`.
    assert shows[0].dia_slug == "04-set"
    assert shows[0].url.endswith("/line-up/foo-fighters/")


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


def test_urls_dos_dias_usam_o_prefixo_localizado():
    assert scraper_mod.DAY_URL_TEMPLATE == (
        "https://rockinrio.com/rio/pt-br/line-up/dia/{slug}/"
    )


@pytest.mark.asyncio
async def test_buscar_lineup_junta_os_sete_dias(monkeypatch):
    paginas = {
        slug: _pagina(
            _palco("Palco Mundo")
            + _artista(f"banda-{slug}", f"BANDA {slug}")
            + _enchimento(slug)
        )
        for slug, _ in DIAS_DO_EVENTO
    }

    async def baixar(_client, dia_slug):
        return paginas[dia_slug]

    monkeypatch.setattr(scraper_mod, "_baixar_dia", baixar)

    shows = await scraper_mod.buscar_lineup()

    assert len(shows) == 7 * (1 + MIN_ATRACOES_POR_DIA)
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
        return _pagina(_palco("Palco Mundo") + _enchimento())

    monkeypatch.setattr(scraper_mod, "_baixar_dia", baixar)

    with pytest.raises(ConnectionError):
        await scraper_mod.buscar_lineup()


@pytest.mark.asyncio
async def test_buscar_lineup_propaga_pagina_fora_do_formato(monkeypatch):
    async def baixar(_client, dia_slug):
        if dia_slug == "06-set":
            return "<html><body>o tema do site mudou</body></html>"
        return _pagina(_palco("Palco Mundo") + _enchimento())

    monkeypatch.setattr(scraper_mod, "_baixar_dia", baixar)

    # Os dias anteriores ao 06 parseiam sem problema: a exceção precisa vir da
    # página que mudou, e não de um dia sintético raquítico.
    with pytest.raises(LineupInvalido, match="não encontrado"):
        await scraper_mod.buscar_lineup()
