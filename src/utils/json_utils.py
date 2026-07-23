"""
Utilitários de serialização JSON compartilhados.

Contém um encoder JSON seguro para tipos de data/hora, usado em qualquer ponto
do código que precise serializar payloads potencialmente contendo objetos
`datetime.datetime`, `datetime.date` ou `datetime.time` (por exemplo, valores
retornados por queries no BigQuery com colunas do tipo TIMESTAMP/DATE/TIME).
"""

import datetime
import json


class CustomJSONEncoder(json.JSONEncoder):
    """
    JSON Encoder customizado que sabe como converter objetos
    de data, hora e data/hora do Python para strings no padrão ISO 8601.
    """

    def default(self, obj):
        # Se o objeto for uma instância de datetime, date ou time...
        if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
            # ... converta-o para uma string no formato ISO.
            return obj.isoformat()

        # Para qualquer outro tipo, deixe o encoder padrão fazer o trabalho.
        return super().default(obj)
