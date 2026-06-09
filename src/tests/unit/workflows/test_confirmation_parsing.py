"""Testes de reconhecimento de confirmação afirmativa/negativa (POC1 #297).

Cobre o gap de QA em que o bot não reconhecia "yes" (inglês) nem "👍" (joinha)
como confirmação. Valida o helper compartilhado `parse_affirmation` e os
field_validators dos payloads de confirmação que passam por model_validate.
"""

import pytest
from pydantic import ValidationError

from src.tools.multi_step_service.workflows.reparo_luminaria.models import (
    QuadraEsportesPayload,
)
from src.tools.multi_step_service.workflows.sgrc_components.models import (
    AddressConfirmationPayload,
    TicketDataConfirmationPayload,
    parse_affirmation,
)


@pytest.mark.parametrize(
    "value",
    [
        True,
        "sim",
        "Sim",
        " sim ",
        "s",
        "yes",
        "YES",
        "y",
        "ok",
        "okay",
        "isso",
        "isso mesmo",
        "correto",
        "certo",
        "claro",
        "pode",
        "quero",
        "confirmo",
        "beleza",
        "👍",
        "👍🏽",  # com modificador de tom de pele
        "👍 isso",  # emoji + texto
        "✅",
        "👌",
    ],
)
def test_parse_affirmation_true(value):
    assert parse_affirmation(value) is True


@pytest.mark.parametrize(
    "value",
    [
        False,
        "não",
        "nao",
        "NÃO",
        "n",
        "no",
        "nope",
        "errado",
        "incorreto",
        "negativo",
        "nao quero",
        "discordo",
        "👎",
        "❌",
    ],
)
def test_parse_affirmation_false(value):
    assert parse_affirmation(value) is False


@pytest.mark.parametrize(
    "value",
    [None, "", "talvez", "acho que sim mas nao sei", "depende", "oi"],
)
def test_parse_affirmation_ambiguous_returns_none(value):
    # Ambíguo: cabe ao chamador decidir (re-perguntar / preservar comportamento).
    assert parse_affirmation(value) is None


def test_parse_affirmation_no_false_positive_on_negated_phrase():
    # "nao pode" não pode virar True por conter "pode" (casamento é exato).
    assert parse_affirmation("nao pode") is not True


@pytest.mark.parametrize(
    "value",
    ["não 👍", "nao 👍", "tá errado ✅", "errado 👍", "claro que não 👍"],
)
def test_parse_affirmation_emoji_does_not_override_negation_word(value):
    # O lado caro é confirmar quando o cidadão disse "não": um 👍 junto de uma
    # palavra de negação NÃO confirma — vira ambíguo (re-pergunta).
    assert parse_affirmation(value) is not True


@pytest.mark.parametrize("value", ["sim 👎", "claro 👎", "isso 👎"])
def test_parse_affirmation_emoji_does_not_override_affirmation_word(value):
    assert parse_affirmation(value) is not False


def test_address_confirmation_accepts_yes_and_thumbs():
    assert (
        AddressConfirmationPayload.model_validate({"confirmacao": "yes"}).confirmacao
        is True
    )
    assert (
        AddressConfirmationPayload.model_validate({"confirmacao": "👍"}).confirmacao
        is True
    )
    assert (
        AddressConfirmationPayload.model_validate({"confirmacao": "isso"}).confirmacao
        is True
    )
    assert (
        AddressConfirmationPayload.model_validate({"confirmacao": "não"}).confirmacao
        is False
    )
    assert (
        AddressConfirmationPayload.model_validate({"confirmacao": True}).confirmacao
        is True
    )


def test_address_confirmation_rejects_ambiguous():
    with pytest.raises(ValidationError):
        AddressConfirmationPayload.model_validate({"confirmacao": "talvez"})


def test_ticket_data_confirmation_optional_none_and_tokens():
    # Só `correcao` presente → confirmacao permanece None (não levanta erro).
    only_correcao = TicketDataConfirmationPayload.model_validate(
        {"correcao": "mudar o endereço"}
    )
    assert only_correcao.confirmacao is None
    assert only_correcao.correcao == "mudar o endereço"

    assert (
        TicketDataConfirmationPayload.model_validate({"confirmacao": "ok"}).confirmacao
        is True
    )
    assert (
        TicketDataConfirmationPayload.model_validate({"confirmacao": "👍"}).confirmacao
        is True
    )
    assert (
        TicketDataConfirmationPayload.model_validate(
            {"confirmacao": "errado"}
        ).confirmacao
        is False
    )


def test_quadra_esportes_accepts_natural_confirmation():
    assert (
        QuadraEsportesPayload.model_validate(
            {"reparo_luminaria_quadra_esportes": "sim"}
        ).reparo_luminaria_quadra_esportes
        is True
    )
    assert (
        QuadraEsportesPayload.model_validate(
            {"reparo_luminaria_quadra_esportes": "👍"}
        ).reparo_luminaria_quadra_esportes
        is True
    )
    assert (
        QuadraEsportesPayload.model_validate(
            {"reparo_luminaria_quadra_esportes": "não"}
        ).reparo_luminaria_quadra_esportes
        is False
    )
