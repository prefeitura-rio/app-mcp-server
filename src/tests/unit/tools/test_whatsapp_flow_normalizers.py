"""
Testes pro normalizer de prefill (workflow → Flow JSON IDs).

Cobre `reparo_luminaria` — único service com normalizer custom hoje.
Resto: pass-through filtrado.
"""

from src.tools.whatsapp_flows.normalizers import normalize_prefill_for_flow


# ─────────────────────────────────────────────────────────────────────
# reparo_luminaria
# ─────────────────────────────────────────────────────────────────────


def test_luminaria_workflow_keys_translated():
    """workflow chaves pt-BR → Flow IDs canônicos."""
    raw = {
        "luminaria_defeito": "Apagada",
        "luminaria_localizacao": "Rua",
    }
    out = normalize_prefill_for_flow("reparo_luminaria", raw)
    assert out["defect_type"] == "Apagada"
    assert out["location"] == "Rua"


def test_luminaria_canonical_keys_passthrough():
    """LLM passa keys canônicas direto — passa filtrado."""
    raw = {"defect_type": "Piscando", "location": "Calçada"}
    out = normalize_prefill_for_flow("reparo_luminaria", raw)
    assert out == raw


def test_luminaria_invalid_defect_dropped():
    """defect_type não-whitelist (e sem alias) é dropado (Meta dropa silencioso).

    NOTA 2026-06-03: "apagado" (masculino) virou alias VÁLIDO de "Apagada"
    — "poste/refletor apagado" é o defeito Apagada. Aqui usamos valores que
    não são defeito nenhum pra cobrir o drop."""
    out = normalize_prefill_for_flow("reparo_luminaria", {"luminaria_defeito": "1"})
    assert "defect_type" not in out

    out = normalize_prefill_for_flow(
        "reparo_luminaria", {"defect_type": "funcionando"}
    )  # não é defeito
    assert "defect_type" not in out


def test_luminaria_defect_case_insensitive_alias():
    """Lowercase + aliases comuns viram ID canônico."""
    for input_val, expected in [
        ("apagada", "Apagada"),
        ("APAGADA", "Apagada"),
        ("acesa durante o dia", "Acesa de dia"),
        ("com ruido", "Com ruído"),
        ("piscando", "Piscando"),
    ]:
        out = normalize_prefill_for_flow("reparo_luminaria", {"defect_type": input_val})
        assert out["defect_type"] == expected, f"{input_val} → {expected}"


def test_luminaria_location_case_insensitive_alias():
    for input_val, expected in [
        ("praca", "Praça"),
        ("praça", "Praça"),
        ("PRAÇA", "Praça"),
        ("calcada", "Calçada"),
        ("nao sei", "Não sei"),
    ]:
        out = normalize_prefill_for_flow("reparo_luminaria", {"location": input_val})
        assert out["location"] == expected, f"{input_val} → {expected}"


def test_luminaria_invalid_location_dropped():
    out = normalize_prefill_for_flow("reparo_luminaria", {"luminaria_localizacao": "7"})
    assert "location" not in out


def test_luminaria_qty_canonical_passthrough():
    """qty_pattern já canônico ('uma'/'bloco'/'intercaladas') passa direto."""
    for v in ("uma", "bloco", "intercaladas"):
        out = normalize_prefill_for_flow("reparo_luminaria", {"qty_pattern": v})
        assert out["qty_pattern"] == v


def test_luminaria_qty_workflow_grupo_bloco():
    """workflow `luminaria_quantidade='grupo' + intercaladas_bloco='bloco'`
    → qty_pattern='bloco'."""
    out = normalize_prefill_for_flow(
        "reparo_luminaria",
        {"luminaria_quantidade": "grupo", "luminaria_intercaladas_bloco": "bloco"},
    )
    assert out["qty_pattern"] == "bloco"


def test_luminaria_qty_workflow_grupo_intercaladas():
    out = normalize_prefill_for_flow(
        "reparo_luminaria",
        {
            "luminaria_quantidade": "grupo",
            "luminaria_intercaladas_bloco": "intercaladas",
        },
    )
    assert out["qty_pattern"] == "intercaladas"


def test_luminaria_qty_workflow_uma():
    """`luminaria_quantidade='uma'` (sem intercaladas) → qty='uma'."""
    out = normalize_prefill_for_flow(
        "reparo_luminaria", {"luminaria_quantidade": "uma"}
    )
    assert out["qty_pattern"] == "uma"


def test_luminaria_qty_workflow_grupo_only_dropped():
    """`grupo` sem intercaladas_bloco → qty dropado (sem suficiente)."""
    out = normalize_prefill_for_flow(
        "reparo_luminaria", {"luminaria_quantidade": "grupo"}
    )
    assert "qty_pattern" not in out


def test_luminaria_full_workflow_payload():
    """Cenário realista: payload completo do workflow → tudo mapeado."""
    raw = {
        "_source": "user_input",
        "luminaria_defeito": "Apagada",
        "luminaria_quantidade": "grupo",
        "luminaria_intercaladas_bloco": "bloco",
        "luminaria_localizacao": "Rua",
    }
    out = normalize_prefill_for_flow("reparo_luminaria", raw)
    assert out == {
        "defect_type": "Apagada",
        "location": "Rua",
        "qty_pattern": "bloco",
    }


def test_luminaria_empty_returns_empty():
    assert normalize_prefill_for_flow("reparo_luminaria", None) == {}
    assert normalize_prefill_for_flow("reparo_luminaria", {}) == {}


# ─────────────────────────────────────────────────────────────────────
# Linguagem natural (2026-06-03): o engine extrai do chat e às vezes manda
# a forma falada em vez do ID canônico. Sem alias o normalizer dropava em
# silêncio → Flow abria vazio.
# ─────────────────────────────────────────────────────────────────────


def test_luminaria_defect_natural_language():
    for input_val, expected in [
        ("sem luz", "Apagada"),
        ("sem iluminação", "Apagada"),
        ("luz apagada", "Apagada"),
        ("não acende", "Apagada"),
        ("queimada", "Apagada"),
        ("intermitente", "Piscando"),
        ("quebrada", "Danificada"),
        ("com barulho", "Com ruído"),
    ]:
        out = normalize_prefill_for_flow("reparo_luminaria", {"defect_type": input_val})
        assert out.get("defect_type") == expected, f"{input_val} → {expected}"


def test_luminaria_qty_natural_language():
    for input_val, expected in [
        ("uma única", "uma"),
        ("única", "uma"),
        ("só uma", "uma"),
        ("apenas uma", "uma"),
        ("1", "uma"),
        ("quarteirão", "bloco"),
        ("seguidas", "bloco"),
        ("alternadas", "intercaladas"),
    ]:
        out = normalize_prefill_for_flow("reparo_luminaria", {"qty_pattern": input_val})
        assert out.get("qty_pattern") == expected, f"{input_val} → {expected}"


def test_luminaria_qty_alias_does_not_break_grupo_workflow():
    """Regressão: 'grupo' NÃO é alias de qty (precisa do sub-campo p/
    desambiguar). 'grupo' sozinho continua dropado."""
    out = normalize_prefill_for_flow(
        "reparo_luminaria", {"luminaria_quantidade": "grupo"}
    )
    assert "qty_pattern" not in out


def test_luminaria_location_strips_leading_preposition():
    """'na calçada' / 'no parque' → noun casado (engine às vezes manda com
    preposição)."""
    for input_val, expected in [
        ("na calçada", "Calçada"),
        ("no parque", "Parque"),
        ("na praça", "Praça"),
        ("na rua", "Rua"),
    ]:
        out = normalize_prefill_for_flow("reparo_luminaria", {"location": input_val})
        assert out.get("location") == expected, f"{input_val} → {expected}"


def test_luminaria_spoken_payload_fully_mapped():
    """Caso real que abria o Flow vazio: 'luminária sem luz, uma única na
    calçada' — antes só `location` sobrevivia; agora os três mapeiam mesmo
    com a preposição em 'na calçada'."""
    raw = {
        "defect_type": "sem luz",
        "qty_pattern": "uma única",
        "location": "na calçada",
    }
    out = normalize_prefill_for_flow("reparo_luminaria", raw)
    assert out == {
        "defect_type": "Apagada",
        "qty_pattern": "uma",
        "location": "Calçada",
    }


# ─────────────────────────────────────────────────────────────────────
# Unknown service
# ─────────────────────────────────────────────────────────────────────


def test_unknown_service_passthrough_filtered():
    """Service desconhecido → pass-through removendo None/empty."""
    raw = {"a": "1", "b": None, "c": "", "d": "valid"}
    out = normalize_prefill_for_flow("poda_arvore_futuro", raw)
    assert out == {"a": "1", "d": "valid"}
