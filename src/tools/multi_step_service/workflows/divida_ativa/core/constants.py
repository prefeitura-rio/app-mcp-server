"""
Constantes para o workflow Dívida Ativa.

Centraliza valores mágicos e configurações do workflow.
"""

# Configurações de Debug / API
FAKE_API_ENV_VAR = "DIVIDA_ATIVA_USE_FAKE_API"

# State Internal Keys
STATE_CONSULTA_REALIZADA = "consulta_realizada"
STATE_TIPO_CONSULTA_CACHE = "tipo_consulta_cache"
STATE_PAYLOAD_ESPERADO = "payload_esperado"

# Mensagens de Erro
ERROR_ENTRADA_AUSENTE = "Documento/identificador não foi coletado corretamente"
