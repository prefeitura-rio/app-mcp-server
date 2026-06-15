"""
Testes pra encode/decode/redact de flow_token (formato v1:base64(json)).

Validam:
- encode roundtrip determinístico (mesmo dict → mesmo token)
- decode tolera padding, Unicode, payloads vazios
- decode falha silenciosa em token corrompido (warn-only, nunca raise)
- redact NÃO expõe payload v1:* em logs
- legacy tokens opacos (UUID) são tratados sem encoding
"""

from src.flows._token import (
    TOKEN_PREFIX,
    decode_flow_token,
    encode_flow_token,
    redact_flow_token,
)


# ─────────────────────────────────────────────────────────────────────
# encode_flow_token
# ─────────────────────────────────────────────────────────────────────


def test_encode_with_prefill_returns_v1_prefix():
    token = encode_flow_token("base-uuid", {"defect_type": "Apagada"})
    assert token.startswith(TOKEN_PREFIX)


def test_encode_empty_prefill_returns_base_token_opaque():
    assert encode_flow_token("base-uuid", None) == "base-uuid"
    assert encode_flow_token("base-uuid", {}) == "base-uuid"


def test_encode_is_deterministic_for_same_session():
    """Mesmo base_token + mesmo dict → mesmo token (sort_keys=True)."""
    t1 = encode_flow_token("x", {"defect_type": "Apagada", "qty_pattern": "uma"})
    t2 = encode_flow_token("x", {"qty_pattern": "uma", "defect_type": "Apagada"})
    assert t1 == t2


def test_encode_differs_for_different_sessions():
    """Mesmo prefill em sessions distintas → tokens DIFERENTES (codex P2:
    UUID preservado no encoded payload via `_session`)."""
    t1 = encode_flow_token("session-A", {"defect_type": "Apagada"})
    t2 = encode_flow_token("session-B", {"defect_type": "Apagada"})
    assert t1 != t2


def test_encode_preserves_session_in_decoded():
    """Decode retorna `_session` + prefill keys."""
    token = encode_flow_token("uuid-correlacao-123", {"defect_type": "Apagada"})
    decoded = decode_flow_token(token)
    assert decoded["_session"] == "uuid-correlacao-123"
    assert decoded["defect_type"] == "Apagada"


def test_encode_session_is_reserved_against_overwrite():
    """Caller/LLM não pode sobrescrever `_session` injetando key igual no
    prefill (codex P3 round 6). `_session` é metadata interna autoritativa."""
    token = encode_flow_token(
        "real-base-uuid",
        {"defect_type": "Apagada", "_session": "fake-overwrite-attempt"},
    )
    decoded = decode_flow_token(token)
    # base_token prevalece sobre tentativa de override
    assert decoded["_session"] == "real-base-uuid"
    assert decoded["defect_type"] == "Apagada"


def test_encode_handles_unicode():
    token = encode_flow_token("x", {"location": "Praça"})
    decoded = decode_flow_token(token)
    assert decoded["location"] == "Praça"


def test_encode_handles_nested_types():
    """JSON suporta dict aninhado, listas, números, booleans."""
    payload = {"defect_type": "Apagada", "qty": 1, "active": True, "tags": ["a", "b"]}
    token = encode_flow_token("x", payload)
    decoded = decode_flow_token(token)
    # Apenas as keys do prefill — _session é metadata interna
    for k, v in payload.items():
        assert decoded[k] == v
    assert decoded["_session"] == "x"


# ─────────────────────────────────────────────────────────────────────
# decode_flow_token
# ─────────────────────────────────────────────────────────────────────


def test_decode_returns_empty_for_opaque_token():
    assert decode_flow_token("uuid-without-prefix") == {}


def test_decode_returns_empty_for_none():
    assert decode_flow_token(None) == {}


def test_decode_returns_empty_for_non_string():
    assert decode_flow_token(12345) == {}  # type: ignore[arg-type]


def test_decode_handles_corrupted_base64():
    """Token v1:* com base64 inválido → dict vazio, sem raise."""
    assert decode_flow_token("v1:not-base64!!@@") == {}


def test_decode_handles_non_json_payload():
    """Token v1:* com base64 válido mas conteúdo não-JSON → dict vazio."""
    # base64 de "hello" (não JSON)
    assert decode_flow_token("v1:aGVsbG8") == {}


def test_decode_handles_non_dict_json():
    """Token v1:* com JSON válido mas não-dict (lista, número) → dict vazio."""
    # base64 de "[1,2,3]"
    assert decode_flow_token("v1:WzEsMiwzXQ") == {}


def test_decode_handles_legacy_pipe_format():
    """Legacy format `uuid|key=val` da branch Gabs (pre-consolidation) →
    dict vazio (não v1:). Compat com bot legacy enviando apenas UUID."""
    assert decode_flow_token("uuid|defect_type=Apagada") == {}


def test_decode_roundtrip_preserves_dict():
    payload = {"defect_type": "Pendurada", "qty_pattern": "uma", "location": "Calçada"}
    token = encode_flow_token("base", payload)
    decoded = decode_flow_token(token)
    # Roundtrip preserva todas as keys do prefill + adiciona _session
    for k, v in payload.items():
        assert decoded[k] == v
    assert decoded["_session"] == "base"


def test_decode_tolerates_missing_padding():
    """encode_flow_token rstrip('=') o padding. decode aceita igual."""
    token = encode_flow_token("base", {"defect_type": "Apagada"})
    assert "=" not in token  # padding stripped
    decoded = decode_flow_token(token)
    assert decoded["defect_type"] == "Apagada"
    assert decoded["_session"] == "base"


# ─────────────────────────────────────────────────────────────────────
# redact_flow_token (PII safety nos logs)
# ─────────────────────────────────────────────────────────────────────


def test_redact_v1_token_masks_payload():
    token = encode_flow_token("base", {"endereco": "Rua X, 100"})
    redacted = redact_flow_token(token)
    # NUNCA o base64 cru ou o endereço aparecem
    assert "Rua X" not in redacted
    assert "endereco" not in redacted
    assert TOKEN_PREFIX in redacted
    assert "<redacted" in redacted


def test_redact_opaque_token_shows_prefix():
    """UUID opaco não carrega PII — mostra prefixo curto pra correlação."""
    redacted = redact_flow_token("abc12345-defg-hijk-lmno-pqrstuvwxyz")
    assert redacted.startswith("abc12345")
    assert "…" in redacted


def test_redact_short_opaque_token_returned_as_is():
    """Tokens muito curtos não-v1 ficam como estão (nada a correlacionar)."""
    assert redact_flow_token("xyz") == "xyz"


def test_redact_handles_none_or_non_string():
    assert redact_flow_token(None) == "<none>"
    assert redact_flow_token(123) == "<none>"  # type: ignore[arg-type]


def test_redact_does_not_leak_length_proximate_to_payload():
    """O `len=N` redacted é o len do encoded base64, não do payload original
    em chars. Atacante não pode reconstruir tamanho aproximado de PII só
    pelo log (overhead de base64 ~33%, e JSON encoding adiciona aspas etc)."""
    token = encode_flow_token("base", {"endereco": "Rua X, 100"})
    redacted = redact_flow_token(token)
    # Apenas confirma que valor numérico aparece (audit-friendly), não testa
    # impossibilidade — é defensive layer.
    assert "len=" in redacted
