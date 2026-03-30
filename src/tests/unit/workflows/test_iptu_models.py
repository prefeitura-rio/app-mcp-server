import sys

import pytest


iptu_models = sys.modules[
    "src.tools.multi_step_service.workflows.iptu_pagamento.core.models"
]


def test_inscricao_imobiliaria_padding():
    payload = iptu_models.InscricaoImobiliariaPayload.model_validate(
        {"inscricao_imobiliaria": "1234"}
    )
    assert payload.inscricao_imobiliaria == "00001234"


def test_inscricao_imobiliaria_rejects_too_long_value():
    with pytest.raises(ValueError, match="não pode ter mais de 15"):
        iptu_models.InscricaoImobiliariaPayload.model_validate(
            {"inscricao_imobiliaria": "1234567890123456"}
        )


def test_ano_exercicio_accepts_string():
    payload = iptu_models.EscolhaAnoPayload.model_validate({"ano_exercicio": "2025"})
    assert payload.ano_exercicio == 2025


def test_ano_exercicio_rejects_out_of_range():
    with pytest.raises(ValueError, match="Ano de exercício inválido"):
        iptu_models.EscolhaAnoPayload.model_validate({"ano_exercicio": 1999})
