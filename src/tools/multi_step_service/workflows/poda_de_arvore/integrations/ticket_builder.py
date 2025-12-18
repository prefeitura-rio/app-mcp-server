from typing import Tuple

from prefeitura_rio.integrations.sgrc.models import Address, Requester, Phones
from src.tools.multi_step_service.core.models import ServiceState


def build_requester(state: ServiceState) -> Requester:
    phones = Phones()

    if state.data.get("phone"):
        phones.telefone1 = state.data.get("phone")

    return Requester(
        email=state.data.get("email", ""),
        cpf=state.data.get("cpf", ""),
        name=state.data.get("name", ""),
        phones=phones,
    )

def build_address(state: ServiceState) -> Address:
    address_data = state.data.get("address", {})

    # Extrai apenas dígitos do número
    street_number = address_data.get("numero", "1") or "1"
    street_number = "".join(filter(str.isdigit, str(street_number)))
    if not street_number:
        street_number = "1"

    ponto_ref = (
        state.data.get("ponto_referencia", "")
        or address_data.get("ponto_referencia", "")
    )

    return Address(
        street=address_data.get(
            "logradouro_nome_ipp",
            address_data.get("logradouro", "")
        ),
        street_code=address_data.get("logradouro_id_ipp", ""),
        neighborhood=address_data.get(
            "bairro_nome_ipp",
            address_data.get("bairro", "")
        ),
        neighborhood_code=address_data.get("bairro_id_ipp", ""),
        number=street_number,
        locality=ponto_ref,
        zip_code=address_data.get("cep", ""),
    )

def build_ticket_payload(state: ServiceState) -> Tuple[Address, Requester, str]:
    address = build_address(state)
    requester = build_requester(state)
    description = "poda de árvore"

    return address, requester, description