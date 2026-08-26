"""
Extração de valor dos códigos de pagamento (CHATR-164).

As fixtures marcadas como "reais" vieram de uma emissão de produção
(`v2/guiapagamento/emitir/avista`). O PIX e o código de barras da mesma guia
declaram o mesmo valor por caminhos independentes — é o que dá confiança em
usar um como fallback do outro.
"""

import pytest

from src.tools.valores_pagamento import (
    valor_brl_para_numero,
    valor_da_guia,
    valor_do_codigo_de_barras,
    valor_do_pix,
)


# Guia real. Campo 54 do EMV: "4825.43".
PIX_REAL = (
    "00020101021226850014br.gov.bcb.pix2563pix.santander.com.br/qr/v2/"
    "a2ecf2b0-c305-4a4c-8cb4-40561cff7e0b520400005303986540748"
    "25.435802BR5917PM RIO DE JANEIRO6014RIO DE JANEIRO62070503***63044CD6"
)

# Mesma guia, linha digitável. Posições 5-15 do código: "00000482543".
BARRAS_REAL = "81650000048-3 25433659202-0 60831418100-9 11098057426-0"

VALOR_REAL = 4825.43

# EMV bem formado sem o campo 54 — o QR em que o pagador digita o valor.
PIX_SEM_VALOR = "00020153039865802BR6304ABCD"


def test_pix_real_traz_o_valor_do_campo_54():
    assert valor_do_pix(PIX_REAL) == VALOR_REAL


def test_codigo_de_barras_real_traz_o_mesmo_valor_do_pix():
    """As duas fontes descrevem a mesma guia; divergir seria bug em uma delas."""
    assert valor_do_codigo_de_barras(BARRAS_REAL) == VALOR_REAL


def test_codigo_de_barras_aceita_as_44_posicoes_sem_digito_verificador():
    sem_dv = "81650000048254336592026083141810011098057426"
    assert valor_do_codigo_de_barras(sem_dv) == VALOR_REAL


def test_pix_sem_campo_54_nao_tem_valor():
    assert valor_do_pix(PIX_SEM_VALOR) is None


@pytest.mark.parametrize(
    "pix",
    [
        "",
        "   ",
        None,
        123,
        "lixo",
        # Tamanho declarado maior do que o que sobra na string.
        "0002015499999",
        # Tamanho não numérico onde o EMV exige dois dígitos.
        "0002015abc12",
    ],
)
def test_pix_invalido_nao_levanta(pix):
    """Um PIX malformado não pode derrubar a emissão de uma guia pagável."""
    assert valor_do_pix(pix) is None


@pytest.mark.parametrize("identificador", ["7", "9"])
def test_codigo_com_valor_de_referencia_nao_devolve_valor(identificador):
    """
    Posição 3 em 7 ou 9 diz que as posições 5-15 são quantidade de moeda de
    referência, não reais. Devolver esse número mostraria valor errado ao
    cidadão.
    """
    codigo = "81" + identificador + "50000048254336592026083141810011098057426"
    assert valor_do_codigo_de_barras(codigo) is None


def test_codigo_de_outro_produto_e_ignorado():
    """Produto diferente de 8 (arrecadação) tem layout diferente."""
    codigo = "31650000048254336592026083141810011098057426"
    assert valor_do_codigo_de_barras(codigo) is None


@pytest.mark.parametrize(
    "codigo",
    ["", None, 42, "123", "8165000004825433659202608314181001109805742699999"],
)
def test_codigo_de_barras_invalido_nao_levanta(codigo):
    assert valor_do_codigo_de_barras(codigo) is None


def test_valor_da_guia_prefere_o_pix():
    assert valor_da_guia(PIX_REAL, BARRAS_REAL) == VALOR_REAL


def test_valor_da_guia_cai_no_codigo_de_barras():
    assert valor_da_guia(PIX_SEM_VALOR, BARRAS_REAL) == VALOR_REAL


def test_valor_da_guia_sem_nenhuma_fonte():
    """Guia sem valor apurável continua sendo uma guia — só sem valor."""
    assert valor_da_guia("", "") is None


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        # Valores reais da consulta de débitos.
        ("R$26.819,86", 26819.86),
        ("R$1.922,05", 1922.05),
        ("R$24.897,81", 24897.81),
        ("R$673,07", 673.07),
        ("R$0,00", 0.0),
        ("R$ 4.825,43", 4825.43),
        (1922.05, 1922.05),
        (100, 100.0),
    ],
)
def test_valor_brl_para_numero(texto, esperado):
    assert valor_brl_para_numero(texto) == esperado


@pytest.mark.parametrize("texto", ["", "   ", None, "N/A", [], True])
def test_valor_brl_nao_parseavel_vira_none(texto):
    """
    None e não 0.0: zero é um saldo legítimo, e devolvê-lo para um campo que
    não veio diria ao consumidor que o débito está quitado.
    """
    assert valor_brl_para_numero(texto) is None


# --- Valores não finitos ---------------------------------------------------
#
# `float()` aceita "nan", "inf" e "Infinity", e estoura para `inf` com dígitos
# demais. Qualquer um deles atravessaria até a serialização, onde o
# `JSONResponse` do FastAPI (`allow_nan=False`) levanta ValueError: o endpoint
# devolveria 500 com a guia já emitida na PGM, e nada chegaria ao cidadão.


@pytest.mark.parametrize("bruto", ["nan", "NaN", "inf", "-inf", "Infinity"])
def test_pix_com_valor_nao_finito_nao_tem_valor(bruto):
    pix = f"000201{'54'}{len(bruto):02d}{bruto}6304ABCD"
    assert valor_do_pix(pix) is None


# Overflow por excesso de dígitos não é alcançável pelo PIX: o tamanho do campo
# EMV tem 2 dígitos, então o campo 54 para em 99 caracteres e `float` só estoura
# perto de 309. Pelo texto BRL da consulta, que não tem limite, é alcançável.


@pytest.mark.parametrize("texto", ["R$" + "9" * 400, "9" * 400])
def test_valor_brl_que_estoura_o_float_vira_none(texto):
    assert valor_brl_para_numero(texto) is None


@pytest.mark.parametrize("numero", [float("nan"), float("inf"), float("-inf")])
def test_valor_brl_numerico_nao_finito_vira_none(numero):
    assert valor_brl_para_numero(numero) is None


def test_valor_da_guia_nao_finito_cai_no_codigo_de_barras():
    """
    O Pix é a fonte primária, mas um campo 54 impagável não pode encobrir o
    código de barras, que descreve a mesma guia e está íntegro.
    """
    pix_nan = "0002015403nan6304ABCD"
    assert valor_da_guia(pix_nan, BARRAS_REAL) == VALOR_REAL


def test_valor_de_guia_e_sempre_serializavel_em_json():
    """
    O contrato que o resto do sistema assume: `valor` ou é número serializável
    ou é None. `allow_nan=False` é o que o FastAPI usa para renderizar.
    """
    import json

    for pix in ["0002015403nan6304ABCD", "0002015403inf6304ABCD", PIX_REAL, ""]:
        valor = valor_da_guia(pix, "")
        json.dumps({"valor": valor}, allow_nan=False)
