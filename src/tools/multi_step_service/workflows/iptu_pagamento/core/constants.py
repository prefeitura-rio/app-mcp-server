"""
Constantes para o workflow IPTU Ano Vigente.

Centraliza valores mágicos e configurações do workflow.
"""

from datetime import datetime

# Validação de Ano de Exercício
ANO_MIN_VALIDO = 2020
ANO_MAX_VALIDO = datetime.now().year

# Validação de Inscrição Imobiliária
INSCRICAO_MIN_LENGTH = 8
INSCRICAO_MAX_LENGTH = 15
INSCRICAO_PATTERN = r"^[0-9]+$"

# Limites e Tentativas
MAX_TENTATIVAS_ANO = 3  # Máximo de tentativas de ano antes de pedir nova inscrição

# Tipos de Guias IPTU
TIPO_GUIA_ORDINARIA = "ORDINÁRIA"
TIPO_GUIA_EXTRAORDINARIA = "EXTRAORDINÁRIA"

# Números de Guias
NUMERO_GUIA_ORDINARIA = "00"
NUMERO_GUIA_EXTRAORDINARIA_01 = "01"
NUMERO_GUIA_EXTRAORDINARIA_02 = "02"

# Situações de Pagamento
SITUACAO_EM_ABERTO = "01"
SITUACAO_QUITADA = "02"
SITUACAO_EM_ABERTO_DESC = "EM ABERTO"
SITUACAO_QUITADA_DESC = "QUITADA"

# Configurações de Debug
DEBUG_MODE_ENV_VAR = "IPTU_DEBUG_MODE"
FAKE_API_ENV_VAR = "IPTU_USE_FAKE_API"

# State Internal Keys (padronizados)
# Flags de estado
STATE_IS_DATA_CONFIRMED = "is_data_confirmed"
STATE_WANTS_MORE_QUOTAS = "wants_more_quotas"
STATE_WANTS_OTHER_GUIAS = "wants_other_guias"
STATE_WANTS_OTHER_PROPERTY = "wants_other_property"
STATE_HAS_CONSULTED_GUIAS = "has_consulted_guias"
STATE_USE_SEPARATE_DARM = "use_separate_darm"
STATE_IS_SINGLE_QUOTA_FLOW = "is_single_quota_flow"

# Tipos de pergunta seguinte
STATE_NEXT_QUESTION_TYPE = "next_question_type"
QUESTION_TYPE_MORE_QUOTAS = "more_quotas"
QUESTION_TYPE_OTHER_GUIAS = "other_guias"
QUESTION_TYPE_OTHER_PROPERTY = "other_property"

# Prefixos para chaves dinâmicas
STATE_FAILED_ATTEMPTS_PREFIX = "failed_attempts_"

# Mensagens de Erro
ERROR_INSCRICAO_AUSENTE = "Inscrição imobiliária não foi coletada corretamente"
ERROR_ANO_AUSENTE = "Ano de exercício não foi coletado corretamente"
ERROR_DADOS_COTAS_AUSENTES = "Dados de cotas não carregados"
ERROR_CAMPO_OBRIGATORIO = "Campo obrigatório faltante"
