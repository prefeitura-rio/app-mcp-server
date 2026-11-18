"""
Detecção de placeholder do SFMC na v2 de Dívida Ativa (DEV-452).

A detecção era o regex `\\{\\{.*?\\}\\}` com DOTALL, que custa O(n²) num campo
com milhares de "{{" e nenhum "}}". Estes testes fixam que a versão linear
reconhece exatamente os mesmos valores que o regex e que uma string hostil é
resolvida rápido, inclusive pela validação de `EmitirGuiaRequest`.
"""

import itertools
import re
import time

import pytest
from pydantic import ValidationError

from src.tools.divida_ativa_v2.models import (
    EmitirGuiaRequest,
    _contem_placeholder,
    _texto_contem_placeholder,
)

# A definição anterior, mantida aqui só como oráculo da equivalência.
REGEX_ANTERIOR = re.compile(r"\{\{.*?\}\}", re.DOTALL)

# Com o regex, 20 mil "{{" sem "}}" já levavam vários segundos; a versão
# linear resolve em microssegundos. O limite é folgado para não oscilar no CI.
LIMITE_SEGUNDOS = 1.0
REPETICOES_HOSTIS = 20_000


def _todas_as_strings(alfabeto: str, tamanho_maximo: int):
    for tamanho in range(tamanho_maximo + 1):
        for letras in itertools.product(alfabeto, repeat=tamanho):
            yield "".join(letras)


def test_equivale_ao_regex_anterior_em_todas_as_strings_curtas():
    """Busca exaustiva: nenhum valor muda de lado (placeholder ou não)."""
    divergentes = [
        texto
        for texto in _todas_as_strings("{}a\n", 7)
        if _texto_contem_placeholder(texto) != bool(REGEX_ANTERIOR.search(texto))
    ]

    assert divergentes == []


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ('{{Event.DEAudience-9f2c."itens_informados"}}', True),
        ("{{}}", True),
        ("antes {{\nquebra de linha\n}} depois", True),
        ("{{{}}}", True),
        ("}} {{", False),
        ("{{}", False),
        ("{}}", False),
        ("{ {x} }", False),
        ("", False),
        ("01/225716/2024-00", False),
    ],
)
def test_casos_de_fronteira(texto, esperado):
    assert _texto_contem_placeholder(texto) is esperado
    assert bool(REGEX_ANTERIOR.search(texto)) is esperado


@pytest.mark.parametrize(
    "hostil",
    [
        "{{" * REPETICOES_HOSTIS,
        "{{a" * REPETICOES_HOSTIS,
        "{{" * REPETICOES_HOSTIS + "}",
    ],
    ids=["so-abre", "abre-com-texto", "fecha-incompleto"],
)
def test_string_hostil_sem_fechamento_e_linear(hostil):
    inicio = time.perf_counter()
    resultado = _contem_placeholder(hostil)
    decorrido = time.perf_counter() - inicio

    assert resultado is False
    assert decorrido < LIMITE_SEGUNDOS


def test_string_hostil_grande_dentro_de_lista_e_dicionario_e_linear():
    hostil = "{{" * 500_000

    inicio = time.perf_counter()
    resultado = _contem_placeholder({"1": [hostil, hostil], hostil: hostil})
    decorrido = time.perf_counter() - inicio

    assert resultado is False
    assert decorrido < LIMITE_SEGUNDOS


def test_string_hostil_com_fechamento_no_fim_continua_placeholder():
    hostil = "{{" * REPETICOES_HOSTIS + "}}"

    inicio = time.perf_counter()
    resultado = _contem_placeholder(hostil)
    decorrido = time.perf_counter() - inicio

    assert resultado is True
    assert decorrido < LIMITE_SEGUNDOS


def test_validacao_da_requisicao_com_campo_hostil_termina_rapido():
    """O caminho de /v2/emitir_guia: o campo é recusado, e sem custo quadrático."""
    hostil = "{{" * REPETICOES_HOSTIS

    inicio = time.perf_counter()
    with pytest.raises(ValidationError) as exc:
        EmitirGuiaRequest.model_validate({"lista_cdas": hostil})
    decorrido = time.perf_counter() - inicio

    assert "placeholder de template" not in str(exc.value)
    assert decorrido < LIMITE_SEGUNDOS
