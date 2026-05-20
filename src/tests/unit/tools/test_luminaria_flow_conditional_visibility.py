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

from src.tools.luminaria_flow import _VISUAL, _compute_visibility

JSON_PATH = (
    pathlib.Path(__file__).parents[3]
    / "tools"
    / "whatsapp_flows"
    / "reparo_luminaria.flow.json"
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


def test_endereco_always_visible(form_children):
    """endereco sempre visível — primeira pergunta."""
    assert "visible" not in form_children["endereco"]


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


# ─── Endpoint logic still canonical ─────────────────────────────────


def test_compute_visibility_visual_no_qty():
    """Lógica Python: visual sem qty → mostra qty, esconde location."""
    for defect in _VISUAL:
        show_qty, show_loc = _compute_visibility(defect, None)
        assert show_qty is True
        assert show_loc is False


def test_compute_visibility_non_visual():
    """Lógica Python: non-visual → esconde qty, mostra location."""
    for defect in ["Pendurada", "Danificada", "Com ruído"]:
        show_qty, show_loc = _compute_visibility(defect, None)
        assert show_qty is False
        assert show_loc is True


def test_compute_visibility_visual_with_qty():
    """Lógica Python: visual + qty selecionado → mostra ambos."""
    show_qty, show_loc = _compute_visibility("Apagada", "uma")
    assert show_qty is True
    assert show_loc is True


# ─── JSON structure invariants ──────────────────────────────────────


def test_init_values_preserved(flow_json):
    """Form init-values devem permanecer (prefill — ganho ADR-026 original)."""
    children = flow_json["screens"][0]["layout"]["children"]
    form = next(c for c in children if c.get("type") == "Form")
    assert "init-values" in form
    for field in ("defect_type", "qty_pattern", "location", "endereco"):
        assert field in form["init-values"]
