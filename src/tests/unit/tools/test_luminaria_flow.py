"""
Testes unitários para luminaria_flow.

Testa handler INIT com pre-fill de WhatsApp Flow.
"""

from src.tools.luminaria_flow import _handle_init


def test_handle_init_with_full_prefill():
    """Testa handler INIT com todas as entidades de pre-fill."""
    payload = {
        "flow_token": "uuid|defect_type=Pendurada|qty_pattern=uma|location=Calçada"
    }

    response = _handle_init(payload)

    assert response["version"] == "3.0"
    assert response["screen"] == "MAIN"
    assert response["data"]["defect_type_prefill"] == "Pendurada"
    assert response["data"]["qty_pattern_prefill"] == "uma"
    assert response["data"]["location_prefill"] == "Calçada"
    assert response["data"]["show_qty_pattern"] is False  # Pendurada não é visual
    assert response["data"]["show_location"] is True  # Tem defeito


def test_handle_init_with_visual_defect():
    """Testa handler INIT com defeito visual (Apagada/Piscando/Acesa de dia)."""
    payload = {"flow_token": "uuid|defect_type=Apagada|qty_pattern=bloco"}

    response = _handle_init(payload)

    assert response["data"]["defect_type_prefill"] == "Apagada"
    assert response["data"]["qty_pattern_prefill"] == "bloco"
    assert response["data"]["show_qty_pattern"] is True  # Apagada é visual
    assert response["data"]["show_location"] is True  # Tem defeito e qty_pattern


def test_handle_init_with_piscando():
    """Testa handler INIT com defeito Piscando."""
    payload = {"flow_token": "uuid|defect_type=Piscando|qty_pattern=intercaladas"}

    response = _handle_init(payload)

    assert response["data"]["defect_type_prefill"] == "Piscando"
    assert response["data"]["show_qty_pattern"] is True  # Piscando é visual


def test_handle_init_with_acesa_de_dia():
    """Testa handler INIT com defeito Acesa de dia."""
    payload = {
        "flow_token": "uuid|defect_type=Acesa de dia|qty_pattern=uma|location=Praça"
    }

    response = _handle_init(payload)

    assert response["data"]["defect_type_prefill"] == "Acesa de dia"
    assert response["data"]["location_prefill"] == "Praça"
    assert response["data"]["show_qty_pattern"] is True  # Acesa de dia é visual


def test_handle_init_without_prefill():
    """Testa handler INIT sem pre-fill (flow_token simples)."""
    payload = {"flow_token": "simple-uuid-without-entities"}

    response = _handle_init(payload)

    assert response["version"] == "3.0"
    assert response["screen"] == "MAIN"
    assert response["data"]["defect_type_prefill"] is None
    assert response["data"]["qty_pattern_prefill"] is None
    assert response["data"]["location_prefill"] is None
    assert response["data"]["show_qty_pattern"] is False
    assert response["data"]["show_location"] is False


def test_handle_init_partial_prefill_only_defect():
    """Testa handler INIT com apenas defect_type (sem qty_pattern e location)."""
    payload = {"flow_token": "uuid|defect_type=Danificada"}

    response = _handle_init(payload)

    assert response["data"]["defect_type_prefill"] == "Danificada"
    assert response["data"]["qty_pattern_prefill"] is None
    assert response["data"]["location_prefill"] is None
    assert response["data"]["show_qty_pattern"] is False  # Danificada não é visual
    assert response["data"]["show_location"] is True  # Tem defeito


def test_handle_init_partial_prefill_visual_without_qty():
    """Testa handler INIT com defeito visual mas sem qty_pattern ainda."""
    payload = {"flow_token": "uuid|defect_type=Piscando"}

    response = _handle_init(payload)

    assert response["data"]["defect_type_prefill"] == "Piscando"
    assert response["data"]["qty_pattern_prefill"] is None
    assert response["data"]["show_qty_pattern"] is True  # Piscando é visual
    assert response["data"]["show_location"] is False  # Falta qty_pattern


def test_handle_init_with_com_ruido():
    """Testa handler INIT com defeito 'Com ruído'."""
    payload = {"flow_token": "uuid|defect_type=Com ruído|location=Monumento"}

    response = _handle_init(payload)

    assert response["data"]["defect_type_prefill"] == "Com ruído"
    assert response["data"]["location_prefill"] == "Monumento"
    assert response["data"]["show_qty_pattern"] is False  # Com ruído não é visual
    assert response["data"]["show_location"] is True


def test_handle_init_empty_payload():
    """Testa handler INIT com payload vazio."""
    payload = {}

    response = _handle_init(payload)

    # Deve funcionar sem flow_token (pega string vazia)
    assert response["version"] == "3.0"
    assert response["screen"] == "MAIN"
    assert response["data"]["defect_type_prefill"] is None
    assert response["data"]["qty_pattern_prefill"] is None
    assert response["data"]["location_prefill"] is None


def test_handle_init_all_defect_types():
    """Testa handler INIT com todos os tipos de defeito possíveis."""
    defect_types = [
        "Apagada",
        "Piscando",
        "Acesa de dia",
        "Pendurada",
        "Danificada",
        "Com ruído",
    ]
    visual_types = {"Apagada", "Piscando", "Acesa de dia"}

    for defect in defect_types:
        payload = {"flow_token": f"uuid|defect_type={defect}|qty_pattern=uma"}
        response = _handle_init(payload)

        assert response["data"]["defect_type_prefill"] == defect
        is_visual = defect in visual_types
        assert response["data"]["show_qty_pattern"] == is_visual


def test_handle_init_all_locations():
    """Testa handler INIT com todas as localizações possíveis."""
    locations = [
        "Calçada",
        "Fachada",
        "Monumento",
        "Parque",
        "Praça",
        "Quadra de esportes",
    ]

    for location in locations:
        payload = {
            "flow_token": f"uuid|defect_type=Pendurada|qty_pattern=uma|location={location}"
        }
        response = _handle_init(payload)

        assert response["data"]["location_prefill"] == location
