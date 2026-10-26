"""Blacklist de domínios das fontes da tool `google_search` (CHATR-314).

Substitui a variável de ambiente `LINK_BLACKLIST`, que tinha dois problemas:

- comparava por trecho de texto, então `x.com` barrava `netflix.com`, `fedex.com`
  e `dropbox.com`;
- o valor de produção morava no Infisical, fora de qualquer revisão.

Aqui a comparação é por domínio: a entrada casa com o próprio domínio e com os
subdomínios dele (`facebook.com` pega `m.facebook.com`, mas não
`notfacebook.com`). A lista mora em `domain_blacklist.yaml`, versionado no
repositório, e é lida uma única vez no import, como o `query_filter.yaml` do
CHATR-294. Na requisição a checagem é só comparação em memória.

Fora da lista, fixa no código: a página de resultados do Google
(`google.com/search`) nunca é fonte.
"""

import re
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import yaml

from src.utils.log import logger

CONFIG_PATH = Path(__file__).with_name("domain_blacklist.yaml")

# Nome de host em minúsculas: rótulos de letras, dígitos e hífen separados por
# ponto, com pelo menos um ponto. Entrada fora disso (com esquema, caminho ou
# espaço) nunca casaria com nada e indica erro de digitação no YAML.
_HOST_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")

# Regra fixa, fora do YAML: a página de resultados do Google nunca é fonte. Foi
# 7,1% das fontes citadas em produção de 19 a 26/10/2026, e a `LINK_BLACKLIST`
# não tinha como pegá-la sem barrar o próprio link de redirect do grounding, que
# também é do google.com.
MOTIVO_BUSCA_DO_GOOGLE = "google.com/search"


def normalizar_dominio(valor: str | None) -> str:
    """Forma canônica de um domínio: minúsculas, sem espaços nas bordas, sem o
    ponto final do FQDN e sem o `www.` do início."""
    dominio = (valor or "").strip().lower().rstrip(".")
    if dominio.startswith("www."):
        dominio = dominio[len("www.") :]
    return dominio


def parece_dominio(valor: str | None) -> bool:
    """Se o texto, já normalizado, tem forma de nome de host."""
    return bool(_HOST_RE.match(normalizar_dominio(valor)))


def host_da_url(url: str | None) -> str:
    """Host normalizado da URL; vazio quando ela não tem host ou não é válida."""
    try:
        return normalizar_dominio(urlparse(url or "").hostname)
    except ValueError:
        return ""


def eh_pagina_de_busca_do_google(url: str | None) -> bool:
    """URL da página de resultados do Google: `google.com/search`, com ou sem
    `www.` e em qualquer subdomínio do google.com."""
    try:
        parsed = urlparse(url or "")
        host = normalizar_dominio(parsed.hostname)
    except ValueError:
        return False
    eh_google = host == "google.com" or host.endswith(".google.com")
    return eh_google and parsed.path.rstrip("/") == "/search"


class DomainBlacklist:
    """Conjunto de domínios bloqueados, comparados por domínio ou subdomínio.

    A busca percorre os sufixos do host, do mais longo para o mais curto
    (`a.b.globo.com`, `b.globo.com`, `globo.com`, `com`): custo proporcional ao
    número de rótulos do host, não ao tamanho da lista.
    """

    def __init__(self, dominios: Iterable[str]):
        normalizados = []
        for valor in dominios:
            dominio = normalizar_dominio(valor)
            if not _HOST_RE.match(dominio):
                raise ValueError(f"Domínio inválido na blacklist: {valor!r}")
            normalizados.append(dominio)
        self._dominios = frozenset(normalizados)

    def __len__(self) -> int:
        return len(self._dominios)

    def __contains__(self, dominio: str) -> bool:
        return normalizar_dominio(dominio) in self._dominios

    def dominio_bloqueado(self, host: str | None) -> str | None:
        """Entrada da lista que bloqueia o host, ou `None` se ele passa."""
        rotulos = normalizar_dominio(host).split(".")
        for inicio in range(len(rotulos)):
            sufixo = ".".join(rotulos[inicio:])
            if sufixo in self._dominios:
                return sufixo
        return None

    def url_bloqueada(self, url: str | None) -> str | None:
        """Motivo do bloqueio da URL (a entrada da lista que casou, ou a regra fixa
        da busca do Google), ou `None`."""
        if eh_pagina_de_busca_do_google(url):
            return MOTIVO_BUSCA_DO_GOOGLE
        return self.dominio_bloqueado(host_da_url(url))

    def fonte_bloqueada(self, web) -> str | None:
        """Entrada que bloqueia uma fonte do grounding (`chunk.web`) pelo domínio,
        antes de resolver o link, ou `None`.

        Usa `web.domain`. O Gemini API não preenche esse campo (o SDK avisa, e em
        26/10/2026 ele veio `None` em todas as fontes de uma busca real); nesse
        caso vale o `title`, que lá traz o domínio do site (`globo.com` para uma
        página do g1), mas só quando tem forma de domínio: um título comum nunca
        é comparado. O host do `Location` é conferido de novo depois da
        resolução, porque os dois podem faltar ou divergir do destino.
        """
        dominio = getattr(web, "domain", None)
        if not dominio:
            titulo = getattr(web, "title", None)
            dominio = titulo if parece_dominio(titulo) else None
        return self.dominio_bloqueado(dominio) if dominio else None

    def __repr__(self) -> str:
        return f"DomainBlacklist(dominios={len(self._dominios)})"


def _lista_de_textos(valor: object, campo: str) -> list[str]:
    # Seção vazia no YAML (`imprensa:`) chega como `None` e vale lista vazia.
    if valor is None:
        return []
    if not isinstance(valor, list) or not all(isinstance(v, str) for v in valor):
        raise ValueError(f"`{campo}` deve ser uma lista de textos.")
    return valor


def load_domain_blacklist(path: Path = CONFIG_PATH) -> DomainBlacklist:
    """Lê o YAML e monta a blacklist. Arquivo malformado levanta `ValueError`.

    Falhar no boot é deliberado, como no `query_filter`: o arquivo é versionado e
    coberto por teste, então erro aqui é bug de PR. Ignorar uma entrada inválida
    deixaria passar um domínio que a revisão bloqueou.
    """
    dados = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(dados, dict):
        raise ValueError(f"{path.name}: o conteúdo deve ser um mapa.")

    secoes = dados.get("blacklist") or {}
    if not isinstance(secoes, dict):
        raise ValueError(f"{path.name}: `blacklist` deve ser um mapa de seções.")
    dominios = [
        dominio
        for secao, valores in secoes.items()
        for dominio in _lista_de_textos(valores, f"blacklist.{secao}")
    ]

    blacklist = DomainBlacklist(dominios)
    logger.info(f"Blacklist de domínios do google_search: {blacklist!r}")
    return blacklist


domain_blacklist: DomainBlacklist = load_domain_blacklist()
