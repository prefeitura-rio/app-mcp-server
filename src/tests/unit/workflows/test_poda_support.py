import sys

import pytest

from src.tools.multi_step_service.core.models import AgentResponse, ServiceState


poda_models = sys.modules[
    "src.tools.multi_step_service.workflows.poda_de_arvore.models"
]
poda_state_helpers = sys.modules[
    "src.tools.multi_step_service.workflows.poda_de_arvore.state_helpers"
]
ticket_builder = sys.modules[
    "src.tools.multi_step_service.workflows.poda_de_arvore.integrations.ticket_builder"
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


def test_ticket_opened_sets_ticket_state():
    state = ServiceState(user_id="u1", service_name="poda_de_arvore")

    result = poda_state_helpers.ticket_opened(
        state,
        protocol_id="12345",
        description="Chamado aberto com sucesso",
    )

    assert result.data["protocol_id"] == "12345"
    assert result.data["ticket_created"] is True
    assert result.agent_response == AgentResponse(
        description="Chamado aberto com sucesso"
    )


def test_ticket_failed_sets_error_state():
    state = ServiceState(user_id="u1", service_name="poda_de_arvore")

    result = poda_state_helpers.ticket_failed(
        state,
        error_code="API_ERROR",
        description="Falha ao abrir chamado",
        error_message="sem conexão",
    )

    assert result.data["ticket_created"] is False
    assert result.data["error"] == "API_ERROR"
    assert result.agent_response.description == "Falha ao abrir chamado"
    assert result.agent_response.error_message == "sem conexão"


def test_build_requester_includes_user_fields_and_phone():
    state = ServiceState(
        user_id="u1",
        service_name="poda_de_arvore",
        data={
            "email": "user@example.com",
            "cpf": "12345678909",
            "name": "Nome Sobrenome",
            "phone": "21999999999",
        },
    )

    requester = ticket_builder.build_requester(state)

    assert requester.email == "user@example.com"
    assert requester.cpf == "12345678909"
    assert requester.name == "Nome Sobrenome"
    assert requester.phones.telefone1 == "21999999999"


def test_build_address_sanitizes_number_and_prefers_ipp_fields():
    state = ServiceState(
        user_id="u1",
        service_name="poda_de_arvore",
        data={
            "address": {
                "logradouro": "Rua Original",
                "logradouro_nome_ipp": "Rua IPP",
                "logradouro_id_ipp": "123",
                "bairro": "Centro",
                "bairro_nome_ipp": "Bairro IPP",
                "bairro_id_ipp": "456",
                "numero": "10A",
                "cep": "20000-000",
            },
            "ponto_referencia": "Perto da praça",
        },
    )

    address = ticket_builder.build_address(state)

    assert address.street == "Rua IPP"
    assert address.street_code == "123"
    assert address.neighborhood == "Bairro IPP"
    assert address.neighborhood_code == "456"
    assert address.number == "10"
    assert address.locality == "Perto da praça"
    assert address.zip_code == "20000-000"


def test_build_address_defaults_number_to_one_when_missing_digits():
    state = ServiceState(
        user_id="u1",
        service_name="poda_de_arvore",
        data={
            "address": {
                "logradouro": "Rua Sem Numero",
                "bairro": "Centro",
                "numero": "S/N",
            }
        },
    )

    address = ticket_builder.build_address(state)

    assert address.number == "1"
    assert address.street == "Rua Sem Numero"
    assert address.neighborhood == "Centro"


def test_build_ticket_payload_returns_expected_tuple():
    state = ServiceState(
        user_id="u1",
        service_name="poda_de_arvore",
        data={
            "address": {"logradouro": "Rua Teste", "bairro": "Centro", "numero": "5"},
            "email": "user@example.com",
        },
    )

    address, requester, description = ticket_builder.build_ticket_payload(state)

    assert address.street == "Rua Teste"
    assert requester.email == "user@example.com"
    assert description == "poda de árvore"
