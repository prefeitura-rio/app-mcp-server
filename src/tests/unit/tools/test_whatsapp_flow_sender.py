"""
Testes do send_flow_by_service — encoding de prefill no flow_token.

Regressão (2026-05-26): o Flow de luminária é dinâmico (data_api_version
3.0), então o cliente WhatsApp IGNORA `flow_action_payload.data` no INIT e o
endpoint `_handle_init` lê o prefill do `flow_token` decodificado. Antes o
sender mandava um UUID opaco → formulário abria em branco mesmo com prefill
conhecido. O sender agora encoda o prefill normalizado no token.
"""

from unittest.mock import patch

import pytest

import src.tools.whatsapp_flow_sender as sender_mod
from src.flows._token import decode_flow_token
from src.flows.reparo_luminaria.handler import _handle_init
from src.tools.whatsapp_flow_sender import send_flow_by_service


class _FakeSender:
    """Captura os args passados a send_flow, sem HTTP nem env."""

    last: dict = {}

    def __init__(self, *_a, **_kw):
        pass

    async def send_flow(
        self,
        recipient,
        flow_id,
        flow_token=None,
        flow_cta="Abrir",
        prefill_data=None,
    ):
        _FakeSender.last = {"flow_token": flow_token, "prefill_data": prefill_data}
        return {"success": True, "message_id": "m1", "flow_token": flow_token}


@pytest.mark.asyncio
async def test_send_flow_by_service_encodes_prefill_into_token():
    with patch.object(sender_mod, "WhatsAppFlowSender", _FakeSender):
        result = await send_flow_by_service(
            "reparo_luminaria",
            "5521999999999",
            prefill_data={
                "defect_type": "apagada",
                "luminaria_quantidade": "uma",
                "luminaria_localizacao": "rua",
            },
        )

    assert result["success"] is True
    token = _FakeSender.last["flow_token"]
    assert token.startswith("v1:"), "prefill presente deve gerar token v1:encoded"

    decoded = decode_flow_token(token)
    assert decoded["defect_type"] == "Apagada"
    assert decoded["qty_pattern"] == "uma"
    assert decoded["location"] == "Rua"
    assert "_session" in decoded  # correlação cross-session preservada

    # Round-trip: _handle_init renderiza o form pré-preenchido + visibilidade.
    init = _handle_init(None, token)["data"]
    assert init["defect_type_prefill"] == "Apagada"
    assert init["qty_pattern_prefill"] == "uma"
    assert init["location_prefill"] == "Rua"
    assert init["show_qty_pattern"] is True  # visual + qty → ambos visíveis
    assert init["show_location"] is True


@pytest.mark.asyncio
async def test_send_flow_by_service_no_prefill_keeps_opaque_uuid_token():
    with patch.object(sender_mod, "WhatsAppFlowSender", _FakeSender):
        await send_flow_by_service(
            "reparo_luminaria", "5521999999999", prefill_data=None
        )

    token = _FakeSender.last["flow_token"]
    assert not token.startswith("v1:"), "sem prefill → UUID opaco (back-compat)"
    assert decode_flow_token(token) == {}


@pytest.mark.asyncio
async def test_send_flow_by_service_does_not_return_pii_token():
    """O token v1: (com possível PII, ex: endereço) vai SÓ pro payload do
    Meta; o valor retornado (propagado em tool-results sem redaction) é o
    UUID base de correlação, nunca o payload reversível."""
    with patch.object(sender_mod, "WhatsAppFlowSender", _FakeSender):
        result = await send_flow_by_service(
            "reparo_luminaria",
            "5521999999999",
            prefill_data={"defect_type": "apagada", "endereco": "Rua X, 100"},
        )

    # Meta recebeu o token encoded (canal real do prefill dinâmico).
    assert _FakeSender.last["flow_token"].startswith("v1:")
    # Mas o retorno NÃO carrega o payload reversível com PII.
    assert not result["flow_token"].startswith("v1:")
    assert decode_flow_token(result["flow_token"]) == {}
