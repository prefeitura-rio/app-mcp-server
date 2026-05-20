"""
Regression tests pra conditional visibility no Flow JSON estático.

ADR-026 migrou Flow pra estático com prefill mas removeu `data_api_version`
+ endpoint dinâmico. Inicialmente ficou sem conditional visibility — todas
as 4 perguntas apareciam sempre. Fix: adicionar `"visible"` expressions
no JSON (Meta v6.0+ suporta em static).

Estes tests garantem que:
1. JSON tem as expressions corretas pros campos condicionais
2. As expressions Meta DSL espelham a lógica Python `_compute_visibility`
   (referência canônica do comportamento)
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


# ─── Visibility expressions presentes ──────────────────────────────


def test_endereco_always_visible(form_children):
    """endereco não deve ter visible (sempre obrigatório)."""
    assert "visible" not in form_children["endereco"]


def test_defect_type_always_visible(form_children):
    """defect_type sempre visível — primeira pergunta."""
    assert "visible" not in form_children["defect_type"]


def test_qty_pattern_has_conditional_visibility(form_children):
    """qty_pattern visible só pra defeitos visuais."""
    assert "visible" in form_children["qty_pattern"], (
        "Regressão: qty_pattern voltou a ser sempre visível. "
        "ADR-026 fix exige conditional visibility."
    )
    expr = form_children["qty_pattern"]["visible"]
    assert "Apagada" in expr
    assert "Piscando" in expr
    assert "Acesa de dia" in expr


def test_location_has_conditional_visibility(form_children):
    """location visible quando defect_type não-visual OU (visual + qty_pattern)."""
    assert "visible" in form_children["location"], (
        "Regressão: location voltou a ser sempre visível. "
        "ADR-026 fix exige conditional visibility."
    )
    expr = form_children["location"]["visible"]
    # Não-visuais: location aparece direto
    assert "Pendurada" in expr
    assert "Danificada" in expr
    assert "Com ruído" in expr
    # Visuais + qty_pattern: location aparece quando qty selecionada
    assert "qty_pattern" in expr


# ─── Visibility expressions espelham _compute_visibility ────────────


def test_visibility_matches_compute_visibility_visual_defects(form_children):
    """
    _compute_visibility(visual, None) → (qty=True, loc=False).
    Expression Meta deve mostrar qty pra visual, esconder loc se qty vazio.
    """
    for defect in _VISUAL:
        show_qty, show_loc = _compute_visibility(defect, None)
        # show_qty=True quando defect é visual — expression contém esse defect
        qty_expr = form_children["qty_pattern"]["visible"]
        assert defect in qty_expr, f"qty expression missing {defect}"
        # show_loc=False quando visual + sem qty (location oculto inicial)
        assert show_qty is True
        assert show_loc is False


def test_visibility_matches_compute_visibility_non_visual_defects(form_children):
    """
    _compute_visibility(non_visual, _) → (qty=False, loc=True).
    Expression Meta deve mostrar location direto.
    """
    non_visual = ["Pendurada", "Danificada", "Com ruído"]
    for defect in non_visual:
        show_qty, show_loc = _compute_visibility(defect, None)
        assert show_qty is False
        assert show_loc is True
        # Expression location contém esse defect
        loc_expr = form_children["location"]["visible"]
        assert defect in loc_expr, f"location expression missing {defect}"


def test_visibility_matches_compute_visibility_visual_with_qty(form_children):
    """
    _compute_visibility(visual, qty) → (qty=True, loc=True).
    Expression Meta deve mostrar location quando qty_pattern selecionado.
    """
    show_qty, show_loc = _compute_visibility("Apagada", "uma")
    assert show_qty is True
    assert show_loc is True
    # Expression location referencia qty_pattern
    loc_expr = form_children["location"]["visible"]
    assert "qty_pattern" in loc_expr


# ─── JSON structure invariants ──────────────────────────────────────


def test_no_data_api_version(flow_json):
    """Static Flow não deve ter data_api_version (ADR-026)."""
    assert "data_api_version" not in flow_json


def test_init_values_preserved(flow_json):
    """Form init-values devem permanecer (prefill nativo, ADR-026 ganho)."""
    children = flow_json["screens"][0]["layout"]["children"]
    form = next(c for c in children if c.get("type") == "Form")
    assert "init-values" in form
    assert "defect_type" in form["init-values"]
    assert "qty_pattern" in form["init-values"]
    assert "location" in form["init-values"]
    assert "endereco" in form["init-values"]
