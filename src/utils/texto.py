"""Normalização de texto para comparação (CHATR-294 e CHATR-314).

Módulo folha, só stdlib: `src/utils/pii.py` e o filtro de query da
`google_search` comparam suas listas de palavras com a mesma forma canônica.
Antes cada um tinha a sua, com regras sutilmente diferentes (uma tirava o
zero-width space e tratava "°" como "º", a outra não), e o mesmo texto podia
ser lido de um jeito pelo filtro e de outro pelo redator.
"""

import unicodedata


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
