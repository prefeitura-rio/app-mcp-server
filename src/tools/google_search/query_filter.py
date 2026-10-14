"""Filtro de query da tool `google_search` (CHATR-294).

Barreira determinística antes do Typesense e do Gemini. Nasceu de um incidente em
que o bot informou o número eleitoral de um candidato e não o do outro: só prompt
não basta, porque a assimetria vinha da variação do modelo e do buscador.

Cada política é uma estratégia com a mesma interface (`check`), escolhida por
`build_query_filter` a partir das listas configuradas:

- nada configurado → `AllowAllFilter` (a busca opera normalmente);
- só blacklist → `BlacklistFilter`;
- só whitelist → `WhitelistFilter`;
- as duas → `CompositeFilter`: a whitelist decide o que entra e a blacklist ainda
  pode vetar, porque um tema permitido pode vir junto de um termo proibido.

As listas moram em `query_filter.yaml`, versionado no repositório, e são lidas uma
única vez no import. Na requisição o filtro só faz comparação em memória: nada de
I/O no event loop.
"""

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol, Sequence

import yaml

from src.utils.log import logger

CONFIG_PATH = Path(__file__).with_name("query_filter.yaml")

# Teto de caracteres que as expressões da whitelist enxergam. O `re` do Python não
# tem timeout, e uma expressão mal escrita na configuração pode entrar em
# backtracking catastrófico sobre uma entrada longa. Query de busca legítima fica
# muito abaixo disto. A blacklist não usa regex e sempre olha o texto inteiro.
WHITELIST_MAX_CHARS = 1000

REASON_BLACKLIST = "blacklist"
REASON_WHITELIST = "whitelist"


def normalizar(texto: str) -> str:
    """Forma canônica para comparação: minúsculas, sem acento, só letras e dígitos
    separados por um espaço.

    `NFKD` decompõe acentos e também formas de compatibilidade ("ＬＵＬＡ" vira
    "LULA", "1º" vira "1o"). Caracteres de formatação invisíveis (categoria `Cf`,
    como o zero-width space) são removidos em vez de virar espaço: senão "lu<ZWSP>la"
    viraria "lu la" e escaparia do termo "lula".

    O sinal de grau ("°") é tratado como o ordinal ("º"): no teclado os dois são
    trocados o tempo todo ("2° turno"), e o `NFKD` só decompõe o ordinal.
    """
    decomposto = unicodedata.normalize("NFKD", texto.replace("°", "º"))
    limpo = "".join(
        c
        for c in decomposto
        if not unicodedata.combining(c) and unicodedata.category(c) != "Cf"
    ).casefold()
    return " ".join("".join(c if c.isalnum() else " " for c in limpo).split())


@dataclass(frozen=True)
class FilterDecision:
    """Resultado do filtro. `matched` é o termo ou a expressão que decidiu; vai para
    log, span e BigQuery, nunca para a resposta ao agente."""

    allowed: bool
    reason: str | None = None
    matched: str | None = None


ALLOWED = FilterDecision(allowed=True)


class QueryFilter(Protocol):
    def check(self, query: str) -> FilterDecision: ...


class AllowAllFilter:
    """Nenhuma lista configurada: tudo passa."""

    def check(self, query: str) -> FilterDecision:
        return ALLOWED

    def __repr__(self) -> str:
        return "AllowAllFilter()"


class BlacklistFilter:
    """Veta a query que contém algum termo como palavra inteira.

    Sem regex: o texto normalizado ganha espaço nas bordas e o termo é procurado
    entre espaços, o que dá limite de palavra com um `in` simples. Substring pura
    barraria "celular" por causa de "lula" e "tomar" por causa de "omar".
    """

    def __init__(self, terms: Iterable[str]):
        # Termo que normaliza para vazio é descartado: `" " in texto` casaria com
        # qualquer query e bloquearia tudo.
        normalizados = (normalizar(term) for term in terms)
        self._terms = tuple(dict.fromkeys(t for t in normalizados if t))

    def __bool__(self) -> bool:
        return bool(self._terms)

    def check(self, query: str) -> FilterDecision:
        texto = f" {normalizar(query)} "
        for term in self._terms:
            if f" {term} " in texto:
                return FilterDecision(False, REASON_BLACKLIST, term)
        return ALLOWED

    def __repr__(self) -> str:
        return f"BlacklistFilter(termos={len(self._terms)})"


class WhitelistFilter:
    """Só deixa passar a query que casa com alguma expressão (`re.search` sobre o
    texto normalizado)."""

    def __init__(self, patterns: Iterable[str]):
        self._patterns = tuple(_compilar(p) for p in patterns if p.strip())

    def __bool__(self) -> bool:
        return bool(self._patterns)

    def check(self, query: str) -> FilterDecision:
        texto = normalizar(query)[:WHITELIST_MAX_CHARS]
        if any(pattern.search(texto) for pattern in self._patterns):
            return ALLOWED
        return FilterDecision(False, REASON_WHITELIST, None)

    def __repr__(self) -> str:
        return f"WhitelistFilter(expressoes={len(self._patterns)})"


class CompositeFilter:
    """Aplica os filtros em ordem; o primeiro que bloquear decide."""

    def __init__(self, filters: Sequence[QueryFilter]):
        self._filters = tuple(filters)

    def check(self, query: str) -> FilterDecision:
        for query_filter in self._filters:
            decision = query_filter.check(query)
            if not decision.allowed:
                return decision
        return ALLOWED

    def __repr__(self) -> str:
        return f"CompositeFilter({', '.join(map(repr, self._filters))})"


def _compilar(pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as erro:
        raise ValueError(
            f"Expressão inválida na whitelist: {pattern!r} ({erro})"
        ) from erro


def build_query_filter(
    whitelist: Iterable[str], blacklist: Iterable[str]
) -> QueryFilter:
    """Escolhe a estratégia pelas listas preenchidas.

    A blacklist vem primeiro no composto: quando as duas barrariam, o motivo
    registrado é o termo específico, mais útil para revisar falso positivo.
    """
    white = WhitelistFilter(whitelist)
    black = BlacklistFilter(blacklist)
    if white and black:
        return CompositeFilter([black, white])
    if white:
        return white
    if black:
        return black
    return AllowAllFilter()


def _lista_de_textos(valor: object, campo: str) -> list[str]:
    # Chave vazia no YAML (`whitelist:`) chega como `None` e vale lista vazia.
    if valor is None:
        return []
    if not isinstance(valor, list) or not all(isinstance(v, str) for v in valor):
        raise ValueError(f"`{campo}` deve ser uma lista de textos.")
    return valor


def load_query_filter(path: Path = CONFIG_PATH) -> QueryFilter:
    """Lê o YAML e monta o filtro. Arquivo malformado levanta `ValueError`.

    Falhar no boot é deliberado: o arquivo é versionado e coberto por teste, então
    erro aqui é bug de PR. Ignorar silenciosamente uma expressão inválida
    deixaria a whitelist mais restritiva do que o revisado; ignorar o arquivo
    inteiro desligaria o filtro sem ninguém perceber.
    """
    dados = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(dados, dict):
        raise ValueError(f"{path.name}: o conteúdo deve ser um mapa.")

    whitelist = _lista_de_textos(dados.get("whitelist"), "whitelist")

    secoes = dados.get("blacklist") or {}
    if not isinstance(secoes, dict):
        raise ValueError(f"{path.name}: `blacklist` deve ser um mapa de seções.")
    blacklist = [
        termo
        for secao, termos in secoes.items()
        for termo in _lista_de_textos(termos, f"blacklist.{secao}")
    ]

    query_filter = build_query_filter(whitelist, blacklist)
    logger.info(f"Filtro de query do google_search: {query_filter!r}")
    return query_filter


query_filter: QueryFilter = load_query_filter()
