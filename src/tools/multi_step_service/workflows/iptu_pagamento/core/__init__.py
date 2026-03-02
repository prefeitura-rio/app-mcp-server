"""
Core components do workflow IPTU.

Contém modelos, validadores e constantes.
"""

from src.tools.multi_step_service.workflows.iptu_pagamento.core.models import (
    InscricaoImobiliariaPayload,
    EscolhaAnoPayload,
    EscolhaGuiasIPTUPayload,
    EscolhaCotasParceladasPayload,
    EscolhaFormatoDarmPayload,
    ConfirmacaoDadosPayload,
    DadosGuias,
    Guia,
    DadosCotas,
    Cota,
    DadosDarm,
    Darm,
    CotaDarm,
    DadosDividaAtiva,
    CDA,
    EF,
    Parcelamento,
)

from src.tools.multi_step_service.workflows.iptu_pagamento.core.constants import (
    MAX_TENTATIVAS_ANO,
    STATE_FAILED_ATTEMPTS_PREFIX,
    STATE_HAS_CONSULTED_GUIAS,
    FAKE_API_ENV_VAR,
)

__all__ = [
    # Models - Payloads
    "InscricaoImobiliariaPayload",
    "EscolhaAnoPayload",
    "EscolhaGuiasIPTUPayload",
    "EscolhaCotasParceladasPayload",
    "EscolhaFormatoDarmPayload",
    "ConfirmacaoDadosPayload",
    # Models - Data
    "DadosGuias",
    "Guia",
    "DadosCotas",
    "Cota",
    "DadosDarm",
    "Darm",
    "CotaDarm",
    "DadosDividaAtiva",
    "CDA",
    "EF",
    "Parcelamento",
    # Constants
    "MAX_TENTATIVAS_ANO",
    "STATE_FAILED_ATTEMPTS_PREFIX",
    "STATE_HAS_CONSULTED_GUIAS",
    "FAKE_API_ENV_VAR",
]
