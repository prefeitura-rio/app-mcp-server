"""Extração do line-up do Rock in Rio 2026 a partir do site oficial (CHATR-187).

Por que raspagem de HTML e não uma API: o site é WordPress, mas a REST API está
bloqueada — qualquer rota sob `/wp-json/` devolve o HTML da home em vez de JSON,
inclusive as rotas dos post types próprios (`wp/v2/dia/<id>`) que o próprio tema
referencia. Não existe endpoint de agenda. A produção do evento foi contatada e
informou que não tem condições de disponibilizar a grade em outro formato.

O que salva o caso é que as páginas de line-up são renderizadas no servidor: o
HTML já chega completo, sem depender de JavaScript. Por isso aqui basta `httpx`
— nada de browser headless. É também o motivo de este módulo não ter voltado a
usar o `crawl4ai`, removido do projeto no CHATR-177.

O que o site NÃO publica é horário. O `<span>` que guardaria a hora vem vazio e
ainda dentro de um comentário HTML em todos os blocos de artista, e a página de
cada artista traz só o dia. A grade horária existe apenas no app oficial. Logo,
o modelo de dado aqui é `dia + palco + artista` — e qualquer tentativa futura de
acrescentar horário precisa começar por trocar a fonte, não por mexer no parser.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date
from typing import Dict, List, Tuple

from src.utils.http_client import InterceptedHTTPClient
from src.utils.log import logger

BASE_URL = "https://rockinrio.com"
DAY_URL_TEMPLATE = BASE_URL + "/rio/line-up/dia/{slug}/"

# Os sete dias da edição de 2026, na ordem em que acontecem. O mapa slug → data
# é explícito de propósito: o slug do site (`04-set`) não carrega o ano, e
# derivar a data por parsing do slug deixaria o código dependente do idioma da
# abreviação do mês. Os dias 08, 09 e 10 não existem — o festival tem um
# intervalo no meio, e é isso que obriga `hoje` a distinguir "estamos no
# festival" de "hoje tem show" (ver `tool.py`).
DIAS_DO_EVENTO: Tuple[Tuple[str, date], ...] = (
    ("04-set", date(2026, 9, 4)),
    ("05-set", date(2026, 9, 5)),
    ("06-set", date(2026, 9, 6)),
    ("07-set", date(2026, 9, 7)),
    ("11-set", date(2026, 9, 11)),
    ("12-set", date(2026, 9, 12)),
    ("13-set", date(2026, 9, 13)),
)

# Timeout por página. O laço de atualização roda em background e o cache absorve
# a latência, então não há pressa; o teto existe só para a task não ficar presa
# num socket que aceitou a conexão e parou de responder.
TIMEOUT_S = 15.0

# Piso de sanidade por dia. Os dias observados têm 22 e 23 atrações; abaixo
# disto o mais provável não é um line-up enxuto, e sim o parser tendo deixado de
# casar com parte da página. Como grade parcial é o pior desfecho possível — o
# chatbot negaria uma banda que está no festival —, o piso derruba o dia inteiro.
# O runner de contrato em `src/tests/e2e/` importa daqui para não manter um
# segundo número em paralelo.
MIN_ATRACOES_POR_DIA = 12

# Teto de tamanho para nome de artista e de palco. O conteúdo vem de um site de
# terceiro e entra direto no contexto do modelo; sem teto, uma alteração no CMS
# injetaria texto arbitrário na conversa com o cidadão. O maior nome observado
# tem menos de 40 caracteres.
MAX_TAMANHO_NOME = 120

# User-Agent de navegador. Sem ele o site responde de forma inconsistente a
# cliente HTTP simples.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# O bloco de resultados começa aqui. Delimitar é essencial e não cosmético: mais
# acima na página existem os links de filtro por palco
# (`/line-up/palco/palco-mundo/`), que casariam com o padrão de artista e
# entrariam na grade como se fossem atrações.
_INICIO_RESULTADO = '<section class="resultado"'

# Cabeçalho de palco. No HTML do site ele não envolve os artistas seguintes —
# é um irmão que os precede. Por isso o agrupamento é por ordem no documento, e
# não por aninhamento: cada artista pertence ao último cabeçalho visto.
_RE_PALCO = re.compile(r'<div class="data"><span>(?P<palco>[^<]*)</span>\s*</div>')

# Bloco de artista. O nome vai do `<h2>` de abertura ao `</h2>` que o fecha.
#
# A âncora de fechamento é o `</h2>`, e não o `<i>` do ícone de seta, por dois
# motivos. O `<i>` é decoração: se o tema deixar de usá-lo, ancorar nele deixa
# de casar e a atração some da grade. E o conteúdo do `<h2>` nem sempre é texto
# puro — o site publica "MEDUZA" com um `<span>` de nota de rodapé no meio do
# nome, que um `[^<]*` não atravessaria.
#
# O corpo é `[^<]*(?:<[^>]*>[^<]*)*?`, e não `.*?`: os dois tokens são
# disjuntos (um começa por `<`, o outro não), então a decomposição é única —
# sem backtracking patológico — e o padrão não consegue atravessar para o bloco
# seguinte como o `.*?` com `DOTALL` atravessava.
_RE_ARTISTA = re.compile(
    r'<a href="(?P<url>' + re.escape(BASE_URL) + r"/rio/pt-br/line-up/"
    r'(?P<slug>[a-z0-9][a-z0-9-]*)/)"'
    r">\s*<h2[^>]*>(?P<artista>[^<]*(?:<[^>]*>[^<]*)*?)</h2>"
)

# Só a âncora do link de artista, sem exigir o `<h2>` que a acompanha. Serve
# para contar quantos blocos de artista o HTML tem e confrontar com quantos o
# parser conseguiu ler — sem esse confronto, um bloco que mudou de forma some da
# grade em silêncio.
_RE_ANCORA_ARTISTA = re.compile(
    r'<a href="' + re.escape(BASE_URL) + r'/rio/pt-br/line-up/[a-z0-9][a-z0-9-]*/"'
)

# Varre palcos e artistas numa passada só. Duas passadas separadas devolveriam
# duas listas sem como intercalá-las de volta — e é exatamente a ordem relativa
# entre elas que carrega a informação de qual artista toca em qual palco.
_RE_ITENS = re.compile(
    "(?P<palco_bloco>"
    + _RE_PALCO.pattern
    + ")|(?P<artista_bloco>"
    + _RE_ARTISTA.pattern
    + ")"
)

# Caracteres invisíveis que vêm colados nos nomes cadastrados no CMS — o site
# publica "AVENGED SEVENFOLD" com um zero-width space no fim. Sem removê-los, o
# nome não bate em comparação nenhuma e ainda vaza para a resposta ao cidadão.
#
# Escritos como escapes, e não como os caracteres em si: um zero-width space
# literal no código-fonte é invisível para quem revisa e some num copiar-colar
# desatento.
_INVISIVEIS = dict.fromkeys(
    ord(c)
    for c in (
        "\u200b",  # zero-width space
        "\u200c",  # zero-width non-joiner
        "\u200d",  # zero-width joiner
        "\u200e",  # left-to-right mark
        "\u200f",  # right-to-left mark
        "\ufeff",  # zero-width no-break space (BOM)
    )
)


# Marcador de nota de rodapé que o site cola no fim de um nome: "MEDUZA" sai do
# CMS como `MEDUZA<span class="fonte-superscript-3">³</span>`. Sai com conteúdo
# e tudo, porque o expoente não faz parte do nome — e a normalização NFKC de
# `_limpar_texto` o converteria em dígito comum, entregando "MEDUZA3" ao cidadão.
_RE_NOTA_DE_RODAPE = re.compile(r'<span class="fonte-superscript[^"]*">[^<]*</span>')


class LineupInvalido(RuntimeError):
    """O HTML baixou, mas não tem a forma esperada.

    É o alarme de "o site mudou". Precisa ser um erro, e não uma lista vazia:
    devolver zero atrações silenciosamente faria o chatbot afirmar que uma banda
    não toca no festival — pior do que admitir que a informação está indisponível.
    """


@dataclass(frozen=True)
class Show:
    """Uma atração, na única granularidade que a fonte oferece.

    Sem campo de horário de propósito — ver o docstring do módulo.
    """

    data: str
    dia_slug: str
    palco: str
    artista: str
    slug: str
    url: str

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)

    def para_resposta(self) -> Dict[str, str]:
        """Projeção que vai para o cache e daí para a resposta da tool.

        `url` e `dia_slug` ficam de fora porque são deriváveis — a URL é o
        `BASE_URL` mais o slug, e o dia é a própria `data` — e, repetidos nas
        156 atrações, custam ~13 KB por chamada, algo como 3,4 mil tokens de
        contexto, sem responder a nenhuma das perguntas que a tool se propõe a
        responder. Os dois campos continuam no `Show`, que é o que o runner de
        contrato consome.
        """
        return {
            "data": self.data,
            "palco": self.palco,
            "artista": self.artista,
            "slug": self.slug,
        }


def url_do_artista(slug: str) -> str:
    """Reconstrói a página do artista no site oficial a partir do slug.

    É por esta derivação existir que `url` não precisa viajar em cada uma das
    156 atrações da resposta (ver `Show.para_resposta`). Fica aqui, ao lado do
    padrão que define a forma da URL, para que as duas nunca saiam de sincronia.
    """
    return f"{BASE_URL}/rio/pt-br/line-up/{slug}/"


def _limpar_texto(bruto: str) -> str:
    """Reduz um trecho de HTML ao texto limpo que vai para a resposta."""
    texto = _RE_NOTA_DE_RODAPE.sub("", bruto)
    texto = re.sub(r"<[^>]+>", "", texto)
    texto = html_lib.unescape(texto)
    texto = texto.translate(_INVISIVEIS)
    # NFKC resolve os espaços e travessões "especiais" que vêm do editor do CMS
    # para as suas formas canônicas, deixando o `split()` abaixo dar conta.
    texto = unicodedata.normalize("NFKC", texto)
    return " ".join(texto.split())


def parse_dia(html: str, *, dia_slug: str, data: date) -> List[Show]:
    """Extrai as atrações de uma página de dia.

    Args:
        html: HTML bruto da página.
        dia_slug: Slug do dia no site (ex.: `04-set`).
        data: Data de calendário correspondente.

    Returns:
        Lista de shows, na ordem em que aparecem na página.

    Raises:
        LineupInvalido: Se a página não tiver a estrutura esperada, se algum
            bloco de artista não puder ser lido, se um nome vier acima do teto
            de tamanho ou se o dia vier abaixo do piso de atrações.
    """
    inicio = html.find(_INICIO_RESULTADO)
    if inicio == -1:
        raise LineupInvalido(
            f"Bloco '{_INICIO_RESULTADO}' não encontrado na página do dia {dia_slug}"
        )

    # Varre do início do bloco de resultados até o fim do documento, sem tentar
    # fechar no `</section>` correspondente. Cortar no primeiro `</section>`
    # parecia mais preciso e era o contrário: bastava o tema passar a inserir uma
    # `<section>` no meio da lista — um banner, um carrossel — para o dia sair
    # truncado pela metade, sem erro nenhum. Os padrões são específicos o
    # bastante para não casarem com nada abaixo do bloco de resultados.
    trecho = html[inicio:]

    shows: List[Show] = []
    palco_atual: str | None = None

    for match in _RE_ITENS.finditer(trecho):
        if match.group("palco_bloco") is not None:
            palco = _limpar_texto(match.group("palco"))
            if len(palco) > MAX_TAMANHO_NOME:
                raise LineupInvalido(
                    f"Nome de palco com {len(palco)} caracteres na página do dia "
                    f"{dia_slug}, acima do teto de {MAX_TAMANHO_NOME}"
                )
            if palco:
                palco_atual = palco
            continue

        if palco_atual is None:
            # Artista antes de qualquer cabeçalho de palco significa que o
            # agrupamento por ordem no documento deixou de valer — ou seja, a
            # premissa central deste parser caiu.
            raise LineupInvalido(
                f"Artista '{match.group('slug')}' aparece antes de qualquer palco "
                f"na página do dia {dia_slug}"
            )

        artista = _limpar_texto(match.group("artista"))
        if not artista:
            raise LineupInvalido(
                f"Artista sem nome (slug '{match.group('slug')}') no dia {dia_slug}"
            )
        if len(artista) > MAX_TAMANHO_NOME:
            raise LineupInvalido(
                f"Nome de artista com {len(artista)} caracteres (slug "
                f"'{match.group('slug')}') no dia {dia_slug}, acima do teto de "
                f"{MAX_TAMANHO_NOME}"
            )

        shows.append(
            Show(
                data=data.isoformat(),
                dia_slug=dia_slug,
                palco=palco_atual,
                artista=artista,
                slug=match.group("slug"),
                url=match.group("url"),
            )
        )

    if not shows:
        raise LineupInvalido(f"Nenhuma atração encontrada na página do dia {dia_slug}")

    # Confronta o que foi lido com o número de blocos de artista presentes no
    # HTML. É o que transforma "um bloco mudou de forma e não casou" em erro:
    # sem isso, o dia sairia com uma atração a menos e ninguém saberia.
    ancoras = len(_RE_ANCORA_ARTISTA.findall(trecho))
    if ancoras != len(shows):
        raise LineupInvalido(
            f"A página do dia {dia_slug} tem {ancoras} blocos de artista, mas o "
            f"parser leu {len(shows)}: o formato do bloco mudou"
        )

    if len(shows) < MIN_ATRACOES_POR_DIA:
        raise LineupInvalido(
            f"Apenas {len(shows)} atrações no dia {dia_slug}, abaixo do piso de "
            f"{MIN_ATRACOES_POR_DIA}: a página provavelmente veio incompleta"
        )

    return shows


async def _baixar_dia(client: InterceptedHTTPClient, dia_slug: str) -> str:
    """Baixa uma página de dia.

    `intercept_errors=False` porque quem chama é, na maior parte do tempo, o
    laço de atualização em background. Com o padrão ligado, um site fora do ar
    vira sete relatórios ao sistema de monitoramento a cada ciclo de 15 minutos
    — centenas por dia, por réplica, para uma indisponibilidade de terceiro que
    o cache já absorve e que o `logger.warning` do laço já registra. Mesmo
    critério de `src/tools/google_search/gemini_service.py`.
    """
    url = DAY_URL_TEMPLATE.format(slug=dia_slug)
    response = await client.get(
        url, headers={"User-Agent": _USER_AGENT}, intercept_errors=False
    )
    response.raise_for_status()
    return response.text


async def buscar_lineup() -> List[Show]:
    """Baixa e faz o parse dos sete dias.

    É tudo ou nada: se um único dia falhar, a função levanta. Entregar a grade
    parcial seria o pior desfecho possível — o chatbot responderia com convicção
    que uma banda não está no festival só porque a página dela não baixou.

    Raises:
        LineupInvalido: Se alguma página vier fora do formato esperado.
        Exception: Erros de rede/HTTP propagados do cliente.
    """
    async with InterceptedHTTPClient(
        user_id="sistema",
        source={"source": "mcp", "tool": "rock_in_rio_lineup"},
        timeout=TIMEOUT_S,
        follow_redirects=True,
    ) as client:
        paginas = await asyncio.gather(
            *(_baixar_dia(client, slug) for slug, _ in DIAS_DO_EVENTO)
        )

    shows: List[Show] = []
    for (dia_slug, data), pagina in zip(DIAS_DO_EVENTO, paginas):
        shows.extend(parse_dia(pagina, dia_slug=dia_slug, data=data))

    logger.info(
        f"Line-up do Rock in Rio carregado: {len(shows)} atrações "
        f"em {len(DIAS_DO_EVENTO)} dias"
    )
    return shows
