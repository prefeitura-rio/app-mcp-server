"""Blocos de mensagem prontos para o WhatsApp, um por dia de festival.

A grade é a mesma de `shows`; o que muda aqui é a apresentação. Ela vive no
servidor, e não na cabeça do modelo, por dois motivos.

O primeiro é fidelidade: montar a lista significa redigitar ~25 nomes por dia,
e cada um é uma chance de trocar "Simo Not Simon" por outra coisa ou de perder
uma atração no meio — exatamente a classe de erro que esta tool existe para
evitar. Com o bloco pronto, o modelo copia.

O segundo é constância: o layout combinado com o PO (um palco por item, linha
em branco entre eles, links do app ao final) sai igual em toda resposta, sem
depender de o modelo lembrar da regra no meio de uma mensagem longa.

O alvo é o WhatsApp e só ele: os itens usam "- " porque é o que o aplicativo
transforma em marcador, e não há markdown de tabela ou cabeçalho em lugar
nenhum.
"""

from __future__ import annotations

from datetime import date
from typing import Dict, Iterable, List, Mapping

from src.utils.datetime_utils import _get_month_pt

# Palavras que ligam dois nomes numa mesma atração ("Luísa Sonza CONVIDA Roberto
# Menescal"). O site publica tudo em caixa alta, e capitalizá-las junto com os
# nomes deixaria "Convida", "Canta" — que é como a mensagem não deve sair.
CONECTORES = frozenset(
    {
        "convida",
        "convidam",
        "canta",
        "cantam",
        "toca",
        "tocam",
        "apresenta",
        "apresentam",
        "in",
        "com",
        "e",
        "em",
        "de",
        "da",
        "do",
        "das",
        "dos",
        "no",
        "na",
        "feat",
        "feat.",
        "part",
        "part.",
        "pres",
        "pres.",
    }
)

# Vogais, para reconhecer sigla. "MC CABELINHO" e "RODRIGO DO CN" precisam
# manter "MC" e "CN" em caixa alta; sem isto virariam "Mc" e "Cn".
_VOGAIS = frozenset("aeiouyáàâãéêíóôõúü")

# Nomes que o site publica em caixa alta e que são grafados assim de verdade.
# Sem esta lista eles virariam "Anna" e "Nexz" na mensagem. É uma lista de
# exceção mesmo: não há como distinguir "ANNA" de "MELLY" olhando o texto, só
# conhecendo o artista.
GRAFIAS_PRESERVADAS = frozenset({"ANNA", "NEXZ"})


def _capitalizar(palavra: str) -> str:
    """Sobe a primeira letra de cada parte separada por hífen.

    Hífen separa nome ("NE-YO" é "Ne-Yo"); apóstrofo e ponto, não
    ("MART'NÁLIA" é "Mart'nália", "JOTA.PÊ" é "Jota.pê"). Por isso a subida é
    manual em vez de `str.title()`, que capitaliza depois de qualquer um dos
    três.
    """
    partes = []
    for parte in palavra.split("-"):
        for indice, letra in enumerate(parte):
            if letra.isalpha():
                parte = parte[:indice] + letra.upper() + parte[indice + 1 :]
                break
        partes.append(parte)
    return "-".join(partes)


def _palavra_de_exibicao(palavra: str, primeira: bool, nome_gritado: bool) -> str:
    # Palavra que o site não gritou já veio grafada — "mgk", "Wanda Sá",
    # "BaianaSystem", "pres.". A decisão é por palavra e não pelo nome inteiro
    # porque o site mistura as duas coisas no mesmo nome ("ALOK & FAMILY pres.
    # RAVE THE WORLD").
    if not palavra.isupper():
        return palavra

    # Num nome que o site só gritou em parte ("AR Baby", "MC Taya"), token curto
    # em caixa alta é inicial que o próprio site quis assim.
    if not nome_gritado and len([c for c in palavra if c.isalpha()]) <= 2:
        return palavra

    # Sigla com dígito no meio do próprio token ("GBZ7N") é estilização, não
    # caixa alta de site. "MAROON 5" não cai aqui: o dígito está em outro token.
    if any(c.isdigit() for c in palavra) and any(c.isalpha() for c in palavra):
        return palavra

    # Token curto sem vogal é inicial ("MC", "RD", "CN", "TZ", "PJ"), não
    # palavra gritada.
    letras = [c for c in palavra if c.isalpha()]
    if 2 <= len(letras) <= 4 and not any(c.lower() in _VOGAIS for c in letras):
        return palavra

    minuscula = palavra.lower()
    if not primeira and minuscula.strip(".,;:") in CONECTORES:
        return minuscula
    return _capitalizar(minuscula)


def nome_de_exibicao(nome: str) -> str:
    """Converte o nome publicado pelo site no nome que vai para a mensagem.

    O site é inconsistente — a maioria das atrações vem em caixa alta, mas
    algumas já vêm grafadas ("Wanda Sá", "mgk"), e há nomes que misturam os dois
    no mesmo texto. Quem já veio grafado passa intacto: se o site se deu ao
    trabalho de diferenciar, a grafia é informação, não ruído.
    """
    if nome in GRAFIAS_PRESERVADAS:
        return nome

    palavras = nome.split(" ")
    gritado = nome.isupper()
    return " ".join(
        _palavra_de_exibicao(palavra, primeira=indice == 0, nome_gritado=gritado)
        for indice, palavra in enumerate(palavras)
    )


def _por_palco(shows: Iterable[Mapping[str, str]]) -> Dict[str, List[str]]:
    """Agrupa as atrações do dia por palco, na ordem em que a página as lista."""
    agrupado: Dict[str, List[str]] = {}
    for show in shows:
        agrupado.setdefault(show["palco"], []).append(nome_de_exibicao(show["artista"]))
    return agrupado


def bloco_do_dia(
    data: date,
    shows_do_dia: Iterable[Mapping[str, str]],
    nome_do_evento: str,
    aviso_de_horarios: str,
    app_oficial: Mapping[str, str],
) -> str:
    """Monta a mensagem de um dia, pronta para ser copiada na conversa."""
    itens = [
        f"- {palco}: {', '.join(artistas)}"
        for palco, artistas in _por_palco(shows_do_dia).items()
    ]
    mes = _get_month_pt(data.month).lower()

    partes = [
        f"No dia {data.day} de {mes} do {nome_do_evento}, as atrações são:",
        *itens,
        aviso_de_horarios,
    ]
    partes += [
        f"- {loja}: {url}"
        for loja, url in (
            ("iOS", app_oficial.get("ios")),
            ("Android", app_oficial.get("android")),
        )
        if url
    ]
    # Linha em branco entre todos os itens: é o espaçamento combinado com o PO,
    # e é o que separa os palcos no aplicativo.
    return "\n\n".join(partes)


def textos_por_dia(
    shows: Iterable[Mapping[str, str]],
    datas: Iterable[date],
    nome_do_evento: str,
    aviso_de_horarios: str,
    app_oficial: Mapping[str, str],
) -> Dict[str, str]:
    """Um bloco por dia do festival, indexado pela data ISO.

    Dia sem atração nenhuma não entra: um bloco com o cabeçalho e nenhum palco
    convidaria o modelo a apresentá-lo como se a programação estivesse vazia.
    """
    por_data: Dict[str, List[Mapping[str, str]]] = {}
    for show in shows:
        por_data.setdefault(show["data"], []).append(show)

    return {
        data.isoformat(): bloco_do_dia(
            data,
            por_data[data.isoformat()],
            nome_do_evento,
            aviso_de_horarios,
            app_oficial,
        )
        for data in datas
        if por_data.get(data.isoformat())
    }
