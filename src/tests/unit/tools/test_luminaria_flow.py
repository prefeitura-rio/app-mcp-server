"""
Testes pro handler INIT do WhatsApp Flow luminária com prefill via flow_token.

Valida:
- _handle_init compõe data com prefills (defect_type_prefill etc.)
- smart visibility (show_qty_pattern, show_location) derivada do prefill
- back-compat: token opaco / sem prefill retorna defaults
- explicit show_* override smart visibility
- _compute_visibility com defect_type visual / non-visual / vazio
"""

from src.tools.luminaria_entity_extractor import encode_flow_token
from src.tools.luminaria_flow import _compute_visibility, _handle_init


# ─────────────────────────────────────────────────────────────────────
# _compute_visibility
# ─────────────────────────────────────────────────────────────────────


def test_visibility_visual_defect_shows_qty_pattern():
    """Apagada/Piscando/Acesa de dia → mostra qty_pattern, esconde location
    até qty selecionada."""
    for visual in ("Apagada", "Piscando", "Acesa de dia"):
        show_qty, show_loc = _compute_visibility(visual, None)
        assert show_qty is True, f"{visual} deveria mostrar qty_pattern"
        assert show_loc is False, f"{visual} sem qty: location escondida"


def test_visibility_non_visual_defect_shows_location():
    """Pendurada/Danificada/Com ruído → vai direto pra location, sem qty."""
    for non_visual in ("Pendurada", "Danificada", "Com ruído"):
        show_qty, show_loc = _compute_visibility(non_visual, None)
        assert show_qty is False, f"{non_visual} não deveria mostrar qty"
        assert show_loc is True, f"{non_visual} deveria mostrar location"


def test_visibility_visual_with_qty_shows_both():
    """Visual + qty_pattern selecionado → ambos visíveis (transição completa)."""
    show_qty, show_loc = _compute_visibility("Apagada", "uma")
    assert show_qty is True
    assert show_loc is True


def test_visibility_empty_defect_hides_all():
    show_qty, show_loc = _compute_visibility(None, None)
    assert show_qty is False
    assert show_loc is False


def test_visibility_empty_string_defect_hides_all():
    """Defect_type string vazio é tratado como ausente."""
    show_qty, show_loc = _compute_visibility("", None)
    assert show_qty is False
    assert show_loc is False


# ─────────────────────────────────────────────────────────────────────
# _handle_init
# ─────────────────────────────────────────────────────────────────────


def test_handle_init_no_token_returns_defaults():
    """Sem flow_token → todos prefills None, show_* False."""
    r = _handle_init(None, None)
    assert r["version"] == "3.0"
    assert r["screen"] == "MAIN"
    data = r["data"]
    assert data["defect_type_prefill"] is None
    assert data["qty_pattern_prefill"] is None
    assert data["location_prefill"] is None
    assert data["show_qty_pattern"] is False
    assert data["show_location"] is False


def test_handle_init_opaque_token_returns_defaults():
    """Token sem prefix v1: → tratado como opaco → defaults."""
    r = _handle_init(None, "abc-123-uuid-opaco")
    assert r["data"]["defect_type_prefill"] is None
    assert r["data"]["show_qty_pattern"] is False


def test_handle_init_with_visual_prefill():
    """Prefill defect_type=Apagada → propaga prefill + show_qty_pattern=True."""
    token = encode_flow_token("x", {"defect_type": "Apagada"})
    r = _handle_init(None, token)
    assert r["data"]["defect_type_prefill"] == "Apagada"
    assert r["data"]["show_qty_pattern"] is True
    assert r["data"]["show_location"] is False


def test_handle_init_with_non_visual_prefill():
    """Prefill defect_type=Pendurada → location direto (não-visual)."""
    token = encode_flow_token("x", {"defect_type": "Pendurada"})
    r = _handle_init(None, token)
    assert r["data"]["defect_type_prefill"] == "Pendurada"
    assert r["data"]["show_qty_pattern"] is False
    assert r["data"]["show_location"] is True


def test_handle_init_with_full_visual_prefill():
    """Visual + qty_pattern preenchido → ambos visíveis."""
    token = encode_flow_token("x", {"defect_type": "Piscando", "qty_pattern": "uma"})
    r = _handle_init(None, token)
    assert r["data"]["defect_type_prefill"] == "Piscando"
    assert r["data"]["qty_pattern_prefill"] == "uma"
    assert r["data"]["show_qty_pattern"] is True
    assert r["data"]["show_location"] is True


def test_handle_init_explicit_show_overrides_smart_visibility():
    """Bot pode forçar visibility passando show_qty_pattern direto;
    overrides smart computation."""
    token = encode_flow_token(
        "x", {"defect_type": "Pendurada", "show_qty_pattern": True}
    )
    r = _handle_init(None, token)
    assert r["data"]["show_qty_pattern"] is True  # explicit win
    # Smart layer não roda porque flag explícita veio


def test_handle_init_extra_keys_propagated():
    """Keys além de defect_type/qty/location/show_* viram <key>_prefill."""
    token = encode_flow_token("x", {"extra_field": "valor arbitrário"})
    r = _handle_init(None, token)
    assert r["data"]["extra_field_prefill"] == "valor arbitrário"


def test_handle_init_incoming_data_layer_applied():
    """Camada 2 (incoming_data decriptado) também propaga, mesmo que vazia
    em Flow dinâmico hoje."""
    r = _handle_init({"defect_type_prefill": "Apagada"}, None)
    # Como veio em incoming_data com key já canônica, mantém
    assert r["data"]["defect_type_prefill"] == "Apagada"


def test_handle_init_token_overrides_incoming_data():
    """Layer 3 (token) > Layer 2 (incoming) — última ganha."""
    token = encode_flow_token("x", {"defect_type": "Pendurada"})
    r = _handle_init({"defect_type_prefill": "Apagada"}, token)
    assert r["data"]["defect_type_prefill"] == "Pendurada"


def test_handle_init_location_prefill():
    """Prefill location standalone (sem defect_type) → não computa show_*."""
    token = encode_flow_token("x", {"location": "Calçada"})
    r = _handle_init(None, token)
    assert r["data"]["location_prefill"] == "Calçada"
    # sem defect_type, smart visibility → todos False
    assert r["data"]["show_qty_pattern"] is False
    assert r["data"]["show_location"] is False


def test_handle_init_response_shape_invariant():
    """Response sempre tem version, screen, data — em qualquer caso."""
    for inp in (None, "uuid-opaco", encode_flow_token("x", {"defect_type": "X"})):
        r = _handle_init(None, inp)
        assert "version" in r
        assert "screen" in r
        assert "data" in r
        assert r["version"] == "3.0"
        assert r["screen"] == "MAIN"
