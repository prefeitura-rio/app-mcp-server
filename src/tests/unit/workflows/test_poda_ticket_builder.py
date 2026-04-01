import sys

from src.tools.multi_step_service.core.models import ServiceState


ticket_builder = sys.modules[
    "src.tools.multi_step_service.workflows.poda_de_arvore.integrations.ticket_builder"
]


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
