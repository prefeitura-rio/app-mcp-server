"""
Testes unitários para luminaria_entity_extractor.

Testa encoding/decoding de entidades de pre-fill no flow_token.
"""

from src.tools.luminaria_entity_extractor import decode_flow_token, encode_flow_token


def test_encode_flow_token_with_all_entities():
    """Testa encoding com todas as entidades preenchidas."""
    base_token = "abc-123-uuid"
    entities = {
        "defect_type": "Pendurada",
        "qty_pattern": "uma",
        "location": "Calçada",
    }

    encoded = encode_flow_token(base_token, entities)

    assert (
        encoded == "abc-123-uuid|defect_type=Pendurada|qty_pattern=uma|location=Calçada"
    )


def test_encode_flow_token_with_partial_entities():
    """Testa encoding com apenas algumas entidades (outras são None)."""
    base_token = "uuid-456"
    entities = {
        "defect_type": "Apagada",
        "qty_pattern": "bloco",
        "location": None,  # Não especificado
    }

    encoded = encode_flow_token(base_token, entities)

    # location=None não deve aparecer no token
    assert encoded == "uuid-456|defect_type=Apagada|qty_pattern=bloco"
    assert "location" not in encoded


def test_encode_flow_token_with_no_entities():
    """Testa encoding sem nenhuma entidade (todas None)."""
    base_token = "simple-uuid"
    entities = {
        "defect_type": None,
        "qty_pattern": None,
        "location": None,
    }

    encoded = encode_flow_token(base_token, entities)

    # Deve retornar apenas o UUID base
    assert encoded == "simple-uuid"


def test_decode_flow_token_with_all_entities():
    """Testa decoding de token com todas as entidades."""
    token = "uuid-789|defect_type=Piscando|qty_pattern=intercaladas|location=Fachada"

    decoded = decode_flow_token(token)

    assert decoded == {
        "defect_type": "Piscando",
        "qty_pattern": "intercaladas",
        "location": "Fachada",
    }


def test_decode_flow_token_with_partial_entities():
    """Testa decoding de token com apenas algumas entidades."""
    token = "uuid-abc|defect_type=Danificada"

    decoded = decode_flow_token(token)

    assert decoded == {
        "defect_type": "Danificada",
        "qty_pattern": None,
        "location": None,
    }


def test_decode_flow_token_simple_uuid():
    """Testa decoding de token sem entidades (UUID simples)."""
    token = "simple-uuid-without-entities"

    decoded = decode_flow_token(token)

    assert decoded == {
        "defect_type": None,
        "qty_pattern": None,
        "location": None,
    }


def test_encode_decode_roundtrip():
    """Testa que encode → decode retorna os mesmos dados."""
    base_token = "test-uuid"
    original_entities = {
        "defect_type": "Acesa de dia",
        "qty_pattern": "uma",
        "location": "Praça",
    }

    encoded = encode_flow_token(base_token, original_entities)
    decoded = decode_flow_token(encoded)

    assert decoded == original_entities


def test_decode_ignores_unknown_keys():
    """Testa que decoding ignora chaves desconhecidas no token."""
    token = "uuid|defect_type=Apagada|unknown_key=value|qty_pattern=bloco"

    decoded = decode_flow_token(token)

    # Deve decodificar apenas chaves conhecidas
    assert decoded == {
        "defect_type": "Apagada",
        "qty_pattern": "bloco",
        "location": None,
    }
    assert "unknown_key" not in decoded


def test_encode_with_special_characters_in_values():
    """Testa encoding com valores que contém caracteres especiais."""
    base_token = "uuid"
    entities = {
        "defect_type": "Com ruído",  # tem espaço
        "qty_pattern": "uma",
        "location": "Quadra de esportes",  # tem espaço
    }

    encoded = encode_flow_token(base_token, entities)
    decoded = decode_flow_token(encoded)

    # Deve preservar os valores com espaços
    assert decoded["defect_type"] == "Com ruído"
    assert decoded["location"] == "Quadra de esportes"
