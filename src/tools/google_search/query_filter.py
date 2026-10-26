"""Filtro de query da tool `google_search` (CHATR-294 e CHATR-314).

Barreira determinística antes do Typesense e do Gemini. Nasceu de um incidente em
que o bot informou o número eleitoral de um candidato e não o do outro: só prompt
não basta, porque a assimetria vinha da variação do modelo e do buscador.

A tool chama um `SearchFilter`, que junta duas políticas; a primeira que barra
decide:

1. **sem assunto** (CHATR-314, `SemAssuntoFilter`): barra a mensagem que sozinha
   não tem o que pesquisar (`[ATTACHMENTS]`, `2027`, `Sim`, `Por favor`). Sem a
   seção `sem_assunto` no YAML, fica desligada;
2. **eleitoral** (CHATR-294): whitelist e blacklist.

Cada política é uma estratégia com a mesma interface (`check`). A eleitoral é
escolhida por `build_query_filter` a partir das listas configuradas:

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
REASON_SEM_ASSUNTO = "sem_assunto"

# Ids das regras do `SemAssuntoFilter`, na ordem em que são avaliadas. Vão em
# `FilterDecision.matched` no lugar do termo: dizem qual regra barrou sem repetir o
# texto do cidadão no span.
REGRA_PLACEHOLDER = "placeholder"  # A: vazio ou só token entre colchetes
REGRA_SEM_LETRA = "sem_letra"  # B: nenhuma letra (número, pontuação, emoji)
REGRA_EXPRESSAO = "expressao"  # C: cumprimento ou confirmação da lista fechada
REGRA_VOCABULARIO = "vocabulario"  # D: só palavras do vocabulário sem assunto

MAX_PALAVRAS_PADRAO = 8


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


class SemAssuntoFilter:
    """Barra a mensagem que, sozinha, não tem assunto para pesquisar (CHATR-314).

    Na semana de 19 a 26/10/2026, 6,3% das buscas eram `Sim`, `Ok`, `2027`,
    `[ATTACHMENTS]` ou `Por favor`: o agente repassou a última mensagem do cidadão
    em vez da dúvida. A busca devolvia nada ou uma pesquisa pela palavra solta
    ("what does sim mean"), e custava uma chamada ao Gemini.

    Regras, avaliadas nesta ordem; a primeira que casa decide:

    - A (`placeholder`): sem texto depois de tirar os tokens entre colchetes que o
      canal injeta no lugar de anexo;
    - B (`sem_letra`): nenhuma letra depois de normalizar. Pega número solto,
      pontuação, CPF, data e emoji. `numeros_permitidos` são exceção (`1746` é o
      próprio assunto);
    - C (`expressao`): a mensagem inteira é uma expressão da lista fechada de
      cumprimentos e confirmações;
    - D (`vocabulario`): todas as palavras estão no vocabulário sem assunto, até
      `max_palavras`. Mensagem mais longa passa: a chance de ter assunto cresce e
      o custo de barrar por engano também.

    C e D comparam o texto normalizado, então caixa, acento e pontuação não
    contam. Tudo o que tem uma palavra de fora das listas passa: `Creche`,
    `Documentos` e `Para 2027` dependem da conversa, mas têm assunto.
    """

    def __init__(
        self,
        *,
        placeholder: str | None = None,
        numeros_permitidos: Iterable[str] = (),
        expressoes: Iterable[str] = (),
        vocabulario: Iterable[str] = (),
        max_palavras: int = MAX_PALAVRAS_PADRAO,
    ):
        self._placeholder = (
            _compilar(placeholder, "sem_assunto.placeholder")
            if placeholder and placeholder.strip()
            else None
        )
        self._numeros_permitidos = _normalizados(numeros_permitidos)
        self._expressoes = _normalizados(expressoes)
        self._vocabulario = _normalizados(vocabulario)
        self._max_palavras = max_palavras

    def check(self, query: str) -> FilterDecision:
        texto = query or ""
        if self._placeholder is not None:
            texto = self._placeholder.sub(" ", texto)
        if not texto.strip():
            return FilterDecision(False, REASON_SEM_ASSUNTO, REGRA_PLACEHOLDER)

        normalizado = normalizar(texto)
        if not any(c.isalpha() for c in normalizado):
            palavras = set(normalizado.split())
            if palavras & self._numeros_permitidos:
                return ALLOWED
            return FilterDecision(False, REASON_SEM_ASSUNTO, REGRA_SEM_LETRA)

        if normalizado in self._expressoes:
            return FilterDecision(False, REASON_SEM_ASSUNTO, REGRA_EXPRESSAO)

        palavras = normalizado.split()
        if (
            self._vocabulario
            and len(palavras) <= self._max_palavras
            and all(palavra in self._vocabulario for palavra in palavras)
        ):
            return FilterDecision(False, REASON_SEM_ASSUNTO, REGRA_VOCABULARIO)

        return ALLOWED

    def __repr__(self) -> str:
        return (
            f"SemAssuntoFilter(expressoes={len(self._expressoes)}, "
            f"vocabulario={len(self._vocabulario)}, "
            f"max_palavras={self._max_palavras})"
        )


class SearchFilter:
    """O filtro que a tool chama: sem assunto e eleitoral, nessa ordem.

    `sem_assunto` vazio (`None`) é o comportamento do CHATR-294: só o eleitoral.
    """

    def __init__(self, eleitoral: QueryFilter, sem_assunto: QueryFilter | None = None):
        self.eleitoral = eleitoral
        self.sem_assunto = sem_assunto
        filtros = [eleitoral] if sem_assunto is None else [sem_assunto, eleitoral]
        self._composto = CompositeFilter(filtros)

    def check(self, query: str) -> FilterDecision:
        return self._composto.check(query)

    def __repr__(self) -> str:
        return f"SearchFilter(sem_assunto={self.sem_assunto!r}, eleitoral={self.eleitoral!r})"


def _normalizados(textos: Iterable[str]) -> frozenset[str]:
    # Como na blacklist, o que normaliza para vazio é descartado.
    return frozenset(t for t in (normalizar(texto) for texto in textos) if t)


def _compilar(pattern: str, campo: str = "whitelist") -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as erro:
        raise ValueError(
            f"Expressão inválida em `{campo}`: {pattern!r} ({erro})"
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


def build_sem_assunto_filter(secao: object) -> SemAssuntoFilter | None:
    """Monta o `SemAssuntoFilter` a partir da seção `sem_assunto` do YAML.

    Seção ausente ou vazia devolve `None`: a busca fica como no CHATR-294.
    """
    if secao is None:
        return None
    if not isinstance(secao, dict):
        raise ValueError("`sem_assunto` deve ser um mapa.")
    if not secao:
        return None

    placeholder = secao.get("placeholder")
    if placeholder is not None and not isinstance(placeholder, str):
        raise ValueError("`sem_assunto.placeholder` deve ser um texto.")

    max_palavras = secao.get("max_palavras", MAX_PALAVRAS_PADRAO)
    # `bool` é subclasse de `int`: `max_palavras: true` não pode valer 1.
    if (
        not isinstance(max_palavras, int)
        or isinstance(max_palavras, bool)
        or max_palavras < 1
    ):
        raise ValueError("`sem_assunto.max_palavras` deve ser um inteiro positivo.")

    return SemAssuntoFilter(
        placeholder=placeholder,
        numeros_permitidos=_lista_de_textos(
            secao.get("numeros_permitidos"), "sem_assunto.numeros_permitidos"
        ),
        expressoes=_lista_de_textos(secao.get("expressoes"), "sem_assunto.expressoes"),
        vocabulario=_lista_de_textos(
            secao.get("vocabulario"), "sem_assunto.vocabulario"
        ),
        max_palavras=max_palavras,
    )


def load_query_filter(path: Path = CONFIG_PATH) -> SearchFilter:
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

    query_filter = SearchFilter(
        eleitoral=build_query_filter(whitelist, blacklist),
        sem_assunto=build_sem_assunto_filter(dados.get("sem_assunto")),
    )
    logger.info(f"Filtro de query do google_search: {query_filter!r}")
    return query_filter


query_filter: SearchFilter = load_query_filter()
