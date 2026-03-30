import sys

import pytest


poda_models = sys.modules[
    "src.tools.multi_step_service.workflows.poda_de_arvore.models"
]


def test_nome_payload_normaliza_caps_and_spaces():
    payload = poda_models.NomePayload.model_validate({"name": "  joão   da   silva  "})
    assert payload.name == "João Da Silva"


def test_nome_payload_rejeita_nome_sem_sobrenome():
    with pytest.raises(ValueError, match="nome e sobrenome"):
        poda_models.NomePayload.model_validate({"name": "João"})


def test_email_payload_normaliza_lowercase():
    payload = poda_models.EmailPayload.model_validate(
        {"email": "  TESTE@EXEMPLO.COM  "}
    )
    assert payload.email == "teste@exemplo.com"


def test_email_payload_rejeita_email_invalido():
    with pytest.raises(ValueError, match="Email inválido"):
        poda_models.EmailPayload.model_validate({"email": "email-invalido"})


def test_cpf_payload_strips_formatting():
    payload = poda_models.CPFPayload.model_validate({"cpf": "123.456.789-09"})
    assert payload.cpf == "12345678909"


def test_cpf_payload_accepts_empty_value():
    payload = poda_models.CPFPayload.model_validate({"cpf": ""})
    assert payload.cpf is None


def test_address_data_normalizes_cep():
    payload = poda_models.AddressData.model_validate(
        {
            "logradouro": "Rua X",
            "numero": "10",
            "bairro": "Centro",
            "cep": "22.220-333",
        }
    )
    assert payload.cep == "22220333"


def test_address_data_invalid_cep_becomes_none():
    payload = poda_models.AddressData.model_validate(
        {
            "logradouro": "Rua X",
            "numero": "10",
            "bairro": "Centro",
            "cep": "123",
        }
    )
    assert payload.cep is None
