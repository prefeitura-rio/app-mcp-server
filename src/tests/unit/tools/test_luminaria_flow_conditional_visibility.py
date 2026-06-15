"""
Regression tests pra Flow dinâmico do reparo de luminária.

Arquitetura (pós-ADR-026 revisado 2026-05-20):
- Flow dinâmico (data_api_version="3.0") — endpoint compute conditional visibility
- Prefill via `_handle_init` (response.data populated pelo endpoint)
- Reactive visibility via `data_exchange` action em on-select (defect_type, qty_pattern)
- Field.visible referencia booleano em `data` (Meta DSL constraint:
  static não aceita `${form.X}` em visible/condition; só `${data.X}` ou bool)

Estes tests garantem que JSON + endpoint estão alinhados:
1. data_api_version está presente (Flow dinâmico)
2. Fields condicionais usam `${data.show_*}` que endpoint popula
3. defect_type + qty_pattern têm `on-select-action: data_exchange` pra
   triggar `_handle_defect_type` / `_handle_qty_pattern`
4. init-values preservados (ganho ADR-026 original)
"""

import json
import pathlib

import pytest

from src.flows.reparo_luminaria.handler import _VISUAL, _compute_visibility

JSON_PATH = (
    pathlib.Path(__file__).parents[3] / "flows" / "reparo_luminaria" / "flow.json"
)


@pytest.fixture
def flow_json():
    return json.loads(JSON_PATH.read_text())


@pytest.fixture
def form_children(flow_json):
    """Children do componente Form (skips o TextBody header)."""
    children = flow_json["screens"][0]["layout"]["children"]
    form = next(c for c in children if c.get("type") == "Form")
    return {c.get("name"): c for c in form["children"] if "name" in c}


# ─── Dynamic Flow architecture ──────────────────────────────────────


def test_data_api_version_present(flow_json):
    """Flow dinâmico exige data_api_version (endpoint serve conditional)."""
    assert "data_api_version" in flow_json, (
        "Regressão: Flow perdeu data_api_version. Sem isso, endpoint "
        "/whatsapp-flow/luminaria não é chamado e conditional visibility "
        "para de funcionar (4 perguntas sempre visíveis)."
    )
    assert flow_json["data_api_version"] == "3.0"


def test_screen_data_declares_show_flags(flow_json):
    """screen.data deve declarar show_qty_pattern e show_location booleans."""
    screen_data = flow_json["screens"][0]["data"]
    assert "show_qty_pattern" in screen_data
    assert "show_location" in screen_data
    assert screen_data["show_qty_pattern"]["type"] == "boolean"
    assert screen_data["show_location"]["type"] == "boolean"


# ─── Field visibility expressions ───────────────────────────────────


def test_defect_type_always_visible(form_children):
    """defect_type sempre visível — segunda pergunta."""
    assert "visible" not in form_children["defect_type"]


def test_qty_pattern_visibility_bound_to_data(form_children):
    """qty_pattern.visible referencia data.show_qty_pattern (boolean dinâmico)."""
    visible = form_children["qty_pattern"].get("visible")
    assert visible == "${data.show_qty_pattern}", (
        f"Esperado ${{data.show_qty_pattern}}, got {visible!r}. "
        "Meta DSL: static não aceita ${form.X}; só ${data.X} ou bool."
    )


def test_location_visibility_bound_to_data(form_children):
    """location.visible referencia data.show_location (boolean dinâmico)."""
    visible = form_children["location"].get("visible")
    assert visible == "${data.show_location}"


# ─── Reactive on-select-action wiring ───────────────────────────────


def test_defect_type_triggers_data_exchange(form_children):
    """defect_type on-select dispara data_exchange (trigger=defect_type)."""
    action = form_children["defect_type"].get("on-select-action")
    assert action is not None, (
        "Faltando on-select-action — endpoint não receberia notificação de mudança"
    )
    assert action["name"] == "data_exchange"
    assert action["payload"]["trigger"] == "defect_type"
    assert action["payload"]["defect_type"] == "${form.defect_type}"


def test_qty_pattern_triggers_data_exchange(form_children):
    """qty_pattern on-select dispara data_exchange (trigger=qty_pattern)."""
    action = form_children["qty_pattern"].get("on-select-action")
    assert action is not None
    assert action["name"] == "data_exchange"
    assert action["payload"]["trigger"] == "qty_pattern"


# ─── Endpoint logic: campos sempre visíveis (2026-06-03) ────────────


def test_compute_visibility_always_visible():
    """qty_pattern + location SEMPRE visíveis — qualquer defeito (visual ou
    não), com ou sem qty, e até sem defect_type. Substitui a visibilidade
    condicional antiga: o cidadão preenche TUDO no formulário, sem follow-up
    de texto depois do submit (UX desconexa relatada em campo)."""
    defects = [*_VISUAL, "Pendurada", "Danificada", "Com ruído", None]
    for defect in defects:
        for qty in (None, "uma", "bloco"):
            show_qty, show_loc = _compute_visibility(defect, qty)
            assert show_qty is True, f"qty escondido pra {defect!r}/{qty!r}"
            assert show_loc is True, f"location escondido pra {defect!r}/{qty!r}"


# ─── JSON structure invariants ──────────────────────────────────────


def test_data_exchange_preserves_other_prefills_from_token():
    """
    Bug reportado: user troca opção que estava prefilled → handlers
    perdiam contexto de OUTROS prefills enviados pelo bot.

    Cenário: bot envia defect=Pendurada + qty=uma + location=Calçada via
    flow_token. User troca defect pra Apagada → qty_pattern aparece
    (era hidden em non-visual). Deve estar PRE-PREENCHIDO com "uma".
    """
    from src.flows._token import encode_flow_token
    from src.flows.reparo_luminaria.handler import _handle_defect_type

    token = encode_flow_token(
        "session-uuid-test",
        {
            "defect_type": "Pendurada",
            "qty_pattern": "uma",
            "location": "Calçada",
        },
    )

    # User troca defect_type pra "Apagada" (visual)
    response = _handle_defect_type("Apagada", flow_token=token)
    data = response["data"]

    # Defect_type echo (user's selection)
    assert data["defect_type_prefill"] == "Apagada"
    # qty_pattern + location ORIGINAIS preservados (bot já sabia)
    assert data["qty_pattern_prefill"] == "uma", (
        "Regressão: qty_pattern_prefill foi limpo, perdendo contexto"
    )
    assert data["location_prefill"] == "Calçada"
    # Visibility: qty_pattern + location SEMPRE visíveis agora (2026-06-03)
    assert data["show_qty_pattern"] is True
    assert data["show_location"] is True


def test_data_exchange_handle_qty_preserves_location_prefill():
    """User troca qty_pattern → location_prefill original deve ficar."""
    from src.flows._token import encode_flow_token
    from src.flows.reparo_luminaria.handler import _handle_qty_pattern

    token = encode_flow_token(
        "session-uuid-test-2", {"defect_type": "Apagada", "location": "Calçada"}
    )
    response = _handle_qty_pattern("uma", flow_token=token)
    data = response["data"]

    assert data["qty_pattern_prefill"] == "uma"
    assert data["location_prefill"] == "Calçada"
    assert data["defect_type_prefill"] == "Apagada"


def test_data_exchange_without_token_still_works():
    """Defensive: sem flow_token, handlers ainda retornam estrutura válida."""
    from src.flows.reparo_luminaria.handler import (
        _handle_defect_type,
        _handle_qty_pattern,
    )

    r1 = _handle_defect_type("Pendurada", flow_token=None)
    assert r1["data"]["defect_type_prefill"] == "Pendurada"
    assert r1["data"]["show_location"] is True

    r2 = _handle_qty_pattern("bloco", flow_token=None)
    assert r2["data"]["qty_pattern_prefill"] == "bloco"


def test_qty_handler_uses_incoming_defect_not_token():
    """
    User troca defect_type Pendurada→Apagada, depois seleciona qty_pattern.
    Handler qty_pattern recebe incoming com defect_type=Apagada (user's
    atual). NÃO deve reverter pra Pendurada (token original).
    """
    from src.flows._token import encode_flow_token
    from src.flows.reparo_luminaria.handler import _handle_qty_pattern

    token = encode_flow_token(
        "sess-revert-test", {"defect_type": "Pendurada", "location": "Calçada"}
    )
    incoming = {
        "trigger": "qty_pattern",
        "qty_pattern": "uma",
        "defect_type": "Apagada",  # user trocou
        "location": "Calçada",
    }
    response = _handle_qty_pattern("uma", incoming=incoming, flow_token=token)
    data = response["data"]
    assert data["defect_type_prefill"] == "Apagada", (
        "Regressão: defect_type revertido pro token em qty trigger"
    )


def test_defect_handler_echoes_other_form_fields():
    """User troca defect — campos atuais do form (location, qty_pattern) preservados."""
    from src.flows.reparo_luminaria.handler import _handle_defect_type

    incoming = {
        "trigger": "defect_type",
        "defect_type": "Apagada",
        "qty_pattern": "uma",
        "location": "Praça",
    }
    response = _handle_defect_type("Apagada", incoming=incoming, flow_token=None)
    data = response["data"]
    assert data["location_prefill"] == "Praça"
    assert data["qty_pattern_prefill"] == "uma"


def test_init_values_preserved(flow_json):
    """Form init-values devem permanecer (prefill — ganho ADR-026 original)."""
    children = flow_json["screens"][0]["layout"]["children"]
    form = next(c for c in children if c.get("type") == "Form")
    assert "init-values" in form
    for field in ("defect_type", "qty_pattern", "location"):
        assert field in form["init-values"]
