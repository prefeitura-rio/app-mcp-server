"""
Testes para parse_affirmation e _normalize_confirmation_text.

Cobre os branches de:
- emoji thumbs-up/down com e sem palavra de polaridade oposta
- resposta ambígua (None)
- texto vazio
- token exato afirmativo/negativo
- bool passado direto
- None passado direto
"""

import pytest
from src.tools.multi_step_service.workflows.sgrc_components.models import (
    AddressConfirmationPayload,
    NomePayload,
    TicketDataConfirmationPayload,
    _normalize_confirmation_text,
    parse_affirmation,
)


# ─────────────────────────────────────────────────────────────────────
# _normalize_confirmation_text
# ─────────────────────────────────────────────────────────────────────


def test_normalize_lower():
    assert _normalize_confirmation_text("SIM") == "sim"


def test_normalize_strip():
    assert _normalize_confirmation_text("  sim  ") == "sim"


def test_normalize_acento():
    assert _normalize_confirmation_text("não") == "nao"


def test_normalize_colapsa_espacos():
    assert _normalize_confirmation_text("ta  certo") == "ta certo"


def test_normalize_none_vira_string_vazia():
    assert _normalize_confirmation_text(None) == ""


def test_normalize_int():
    result = _normalize_confirmation_text(42)
    assert isinstance(result, str)


# ─────────────────────────────────────────────────────────────────────
# parse_affirmation — passagem direta de bool/None
# ─────────────────────────────────────────────────────────────────────


def test_bool_true_passa_direto():
    assert parse_affirmation(True) is True


def test_bool_false_passa_direto():
    assert parse_affirmation(False) is False


def test_none_retorna_none():
    assert parse_affirmation(None) is None


# ─────────────────────────────────────────────────────────────────────
# parse_affirmation — tokens afirmativos/negativos exatos
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "texto", ["sim", "s", "yes", "ok", "claro", "confirmo", "beleza", "👍"]
)
def test_tokens_afirmativos(texto):
    assert parse_affirmation(texto) is True


@pytest.mark.parametrize("texto", ["nao", "n", "no", "nope", "errado", "discordo"])
def test_tokens_negativos(texto):
    assert parse_affirmation(texto) is False


# ─────────────────────────────────────────────────────────────────────
# parse_affirmation — emojis
# ─────────────────────────────────────────────────────────────────────


def test_thumbs_up_sem_negacao():
    assert parse_affirmation("👍") is True


def test_thumbs_down_sem_afirmacao():
    assert parse_affirmation("👎") is False


def test_thumbs_up_com_nao_retorna_none():
    """'não 👍' conflita — emoji cede para a negação → ambíguo."""
    assert parse_affirmation("não 👍") is None


def test_thumbs_down_com_sim_retorna_none():
    """'sim 👎' conflita — ambíguo."""
    assert parse_affirmation("sim 👎") is None


def test_check_mark_afirma():
    assert parse_affirmation("✅") is True


def test_x_mark_nega():
    assert parse_affirmation("❌") is False


# ─────────────────────────────────────────────────────────────────────
# parse_affirmation — casos ambíguos (None)
# ─────────────────────────────────────────────────────────────────────


def test_texto_vazio_retorna_none():
    assert parse_affirmation("") is None


def test_texto_irreconhecivel_retorna_none():
    assert parse_affirmation("talvez") is None


def test_texto_muito_longo_ambiguo():
    assert (
        parse_affirmation("eu acho que pode ser que sim mas não tenho certeza") is None
    )


# ─────────────────────────────────────────────────────────────────────
# parse_affirmation — normalização de acentos funciona no texto inteiro
# ─────────────────────────────────────────────────────────────────────


def test_nao_com_acento():
    assert parse_affirmation("não") is False


def test_sim_com_maiuscula():
    assert parse_affirmation("SIM") is True


def test_ok_com_maiuscula():
    assert parse_affirmation("OK") is True


# ─────────────────────────────────────────────────────────────────────
# AddressConfirmationPayload — branch de raise (ambíguo)
# ─────────────────────────────────────────────────────────────────────


def test_address_confirmation_sim():
    p = AddressConfirmationPayload(confirmacao="sim")
    assert p.confirmacao is True


def test_address_confirmation_nao():
    p = AddressConfirmationPayload(confirmacao="nao")
    assert p.confirmacao is False


def test_address_confirmation_ambiguo_levanta():
    with pytest.raises(Exception):
        AddressConfirmationPayload(confirmacao="talvez")


# ─────────────────────────────────────────────────────────────────────
# TicketDataConfirmationPayload — branch None (linha 359)
# ─────────────────────────────────────────────────────────────────────


def test_ticket_confirmation_none_retorna_none():
    p = TicketDataConfirmationPayload(confirmacao=None)
    assert p.confirmacao is None


def test_ticket_confirmation_sim():
    p = TicketDataConfirmationPayload(confirmacao="sim")
    assert p.confirmacao is True


# ─────────────────────────────────────────────────────────────────────
# NomePayload — branches de validação (linhas 168, 176, 179)
# ─────────────────────────────────────────────────────────────────────


def test_nome_none_retorna_none():
    p = NomePayload(name=None)
    assert p.name is None


def test_nome_vazio_retorna_none():
    p = NomePayload(name="   ")
    assert p.name is None


def test_nome_com_numero_levanta():
    with pytest.raises(Exception):
        NomePayload(name="Jo4o Silva")


def test_nome_parte_curta_levanta():
    with pytest.raises(Exception):
        NomePayload(name="A Silva")
