"""
Constantes para o workflow Dívida Ativa.

Centraliza valores mágicos e configurações do workflow.
"""

from enum import StrEnum


class DividaAtivaStateKey(StrEnum):
    """Chaves internas usadas pelo workflow de Dívida Ativa."""

    CONSULTA_REALIZADA = "consulta_realizada"
    TIPO_CONSULTA_CACHE = "tipo_consulta_cache"
    PAYLOAD_ESPERADO = "payload_esperado"
    CURRENT_VIEW = "current_view"


# Configurações de Debug / API
FAKE_API_ENV_VAR = "DIVIDA_ATIVA_USE_FAKE_API"

# State Internal Keys
STATE_CONSULTA_REALIZADA = DividaAtivaStateKey.CONSULTA_REALIZADA
STATE_TIPO_CONSULTA_CACHE = DividaAtivaStateKey.TIPO_CONSULTA_CACHE
STATE_PAYLOAD_ESPERADO = DividaAtivaStateKey.PAYLOAD_ESPERADO
STATE_CURRENT_VIEW = DividaAtivaStateKey.CURRENT_VIEW

# Mensagens de Erro
ERROR_ENTRADA_AUSENTE = "Documento/identificador não foi coletado corretamente"
