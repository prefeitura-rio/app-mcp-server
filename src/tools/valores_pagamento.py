"""
Valores monetários embutidos em códigos de pagamento.

A PGM não devolve o valor da guia emitida — a resposta de
`v2/guiapagamento/emitir/avista` traz só `dataVencimento`, `pdf`,
`arquivoBase64`, `codigoDeBarras` e `codigoQrEMVPix` (CHATR-164). O valor está
dentro de dois desses campos, e é de lá que ele é lido.

Nada aqui é específico de dívida ativa: são os formatos públicos do BR Code
(Pix) e do código de barras de arrecadação, que qualquer fluxo de emissão de
guia pode reusar.
"""

import math
import re
from typing import Iterator, Optional, Tuple, Union


# Campo 54 do BR Code — "Transaction Amount" (Manual de Padrões Pix, BCB).
EMV_ID_VALOR = "54"

# Posição 3 do código de barras de arrecadação identifica o que está nas
# posições 5-15: valor efetivo em Real (6 e 8) ou quantidade de moeda de
# referência (7 e 9). Só os dois primeiros são dinheiro.
BARRAS_VALOR_EFETIVO = ("6", "8")

# Posição 1: 8 é o produto "arrecadação". Um código de outro produto (boleto
# bancário comum, por exemplo) tem layout diferente e as posições abaixo não
# significam a mesma coisa.
BARRAS_PRODUTO_ARRECADACAO = "8"


def _numero_finito(bruto: Union[str, int, float]) -> Optional[float]:
    """
    `float(bruto)` só quando o resultado é dinheiro representável.

    `float()` aceita "nan", "inf" e "Infinity", e estoura para `inf` com
    dígitos demais. Qualquer um deles atravessaria até a serialização, onde o
    `JSONResponse` do FastAPI (`allow_nan=False`) levanta ValueError e devolve
    500 — com a guia já emitida na PGM e nada chegando ao cidadão. Não
    apurável é o que None significa; é o que estes casos são.
    """
    try:
        numero = float(bruto)
    except ValueError:
        return None

    return numero if math.isfinite(numero) else None


def valor_brl_para_numero(texto: object) -> Optional[float]:
    """
    Converte valor em texto BRL para número.

    A consulta de débitos devolve valor já formatado ("R$1.922,05",
    "R$26.819,86"). Vazio, ausente ou não parseável devolve None — nunca 0.0,
    que é um valor legítimo e diria ao consumidor algo falso.
    """
    if isinstance(texto, (int, float)) and not isinstance(texto, bool):
        return _numero_finito(texto)

    if not isinstance(texto, str):
        return None

    limpo = texto.strip()
    if not limpo:
        return None

    # "R$26.819,86" -> "26819.86": ponto é separador de milhar, vírgula é
    # decimal. Remover o símbolo antes evita que o '$' entre no número.
    limpo = re.sub(r"[^\d,.\-]", "", limpo)
    limpo = limpo.replace(".", "").replace(",", ".")

    return _numero_finito(limpo)


def _campos_emv(payload: str) -> Iterator[Tuple[str, str]]:
    """
    Percorre o BR Code no formato EMV: ID(2) + LEN(2) + VALUE(LEN), repetidos.

    Interrompe em qualquer inconsistência de tamanho, em vez de levantar: um
    Pix malformado não pode derrubar a emissão de uma guia que, no resto, está
    perfeitamente pagável.
    """
    posicao = 0
    total = len(payload)

    while posicao + 4 <= total:
        identificador = payload[posicao : posicao + 2]
        tamanho_bruto = payload[posicao + 2 : posicao + 4]

        if not tamanho_bruto.isdigit():
            return

        tamanho = int(tamanho_bruto)
        inicio = posicao + 4
        fim = inicio + tamanho

        if fim > total:
            return

        yield identificador, payload[inicio:fim]
        posicao = fim


def valor_do_pix(pix: object) -> Optional[float]:
    """
    Valor da transação declarado no BR Code (campo 54).

    Pix sem o campo 54 é legítimo — é o QR em que o pagador digita o valor.
    Nesse caso devolve None e cabe ao chamador tentar outra fonte.
    """
    if not isinstance(pix, str) or not pix.strip():
        return None

    for identificador, valor in _campos_emv(pix.strip()):
        if identificador == EMV_ID_VALOR:
            # O campo 54 vem como decimal com ponto ("4825.43"), no padrão
            # EMV — não no formato brasileiro.
            return _numero_finito(valor)

    return None


def valor_do_codigo_de_barras(codigo: object) -> Optional[float]:
    """
    Valor no código de barras de arrecadação (posições 5-15, em centavos).

    Aceita as duas formas em que o código circula: a linha digitável com os 4
    blocos de 11 dígitos mais dígito verificador (48 dígitos), como a PGM
    devolve, e o código de barras puro (44 dígitos).
    """
    if not isinstance(codigo, str):
        return None

    digitos = re.sub(r"\D", "", codigo)

    if len(digitos) == 48:
        # Linha digitável: cada bloco de 11 dígitos é seguido do seu DV, que
        # não faz parte do código de barras.
        digitos = "".join(digitos[i : i + 11] for i in range(0, 48, 12))

    if len(digitos) != 44:
        return None

    if digitos[0] != BARRAS_PRODUTO_ARRECADACAO:
        return None

    if digitos[2] not in BARRAS_VALOR_EFETIVO:
        # Valor de referência não é dinheiro: devolvê-lo como valor da guia
        # mostraria um número errado ao cidadão.
        return None

    try:
        centavos = int(digitos[4:15])
    except ValueError:
        return None

    return centavos / 100


def valor_da_guia(pix: object, codigo_de_barras: object) -> Optional[float]:
    """
    Valor em dinheiro de uma guia emitida, em reais.

    O Pix é a fonte primária: o campo 54 é um decimal explícito, enquanto o
    código de barras depende de contar posições. As duas foram conferidas
    contra a mesma guia real e batem.

    None quando nenhuma das duas fontes resolve — a guia segue válida e
    pagável, apenas sem valor para exibir.
    """
    valor = valor_do_pix(pix)
    if valor is not None:
        return valor

    return valor_do_codigo_de_barras(codigo_de_barras)
