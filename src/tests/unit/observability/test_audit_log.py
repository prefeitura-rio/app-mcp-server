"""
Testes do audit log (C4 do plano-bot-2026 Fase 0).

Cobrem:
- generate_snowflake_id: time-ordered + unico concorrentemente
- hash_user_id: nao retorna telefone raw + determinismo
- redact_pii: CPF/CEP/telefone/email substituidos
- _summarize via decorator: input/output truncados
- audit_log decorator: registra success=True no caminho feliz
- audit_log decorator: registra success=False em erro e re-levanta
- audit_log decorator: suporta sync e async
- record_action: campos canonicos presentes
"""

from __future__ import annotations

import asyncio
import re
import threading

import pytest

from src.observability.audit_log import (
    _DEFAULT_SUMMARY_MAX,
    _default_success_from_result,
    _parse_machine_id,
    audit_log,
    audit_logger,
    generate_snowflake_id,
    hash_user_id,
    record_action,
    redact_pii,
)


# ---------------------------------------------------------------------------
# Helper: captura records emitidos pelo audit_logger sem depender do sink
# stdout. record_action chama `audit_logger.bind(**record).info(...)`, entao
# os campos ficam em `message.record["extra"]` -- so copiar.
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_audit_records():
    records: list[dict] = []

    def sink(message):
        try:
            extra = dict(message.record.get("extra", {}))
        except Exception:
            return
        # Aceita apenas mensagens explicitamente marcadas como audit (em
        # record["extra"]["channel"] == "audit" -- pre-bind do audit_logger
        # global -- alem dos campos especificos do record_action).
        if extra.get("channel") != "audit":
            return
        # Os campos canonicos do audit estao todos em extra (ex:
        # snowflake_id, action_type, etc). Removemos "channel" pra ficar
        # com o record limpo.
        extra.pop("channel", None)
        records.append(extra)

    handler_id = audit_logger.add(sink, level="INFO")
    yield records
    audit_logger.remove(handler_id)


# ---------------------------------------------------------------------------
# Snowflake ID
# ---------------------------------------------------------------------------


def test_generate_snowflake_id_returns_prefixed_string():
    sid = generate_snowflake_id()
    assert isinstance(sid, str)
    assert sid.startswith("snowflake_")
    suffix = sid.removeprefix("snowflake_")
    assert suffix.isdigit()


def test_generate_snowflake_id_time_ordered_under_load():
    """Ids gerados em sequencia rapida sao monotonic crescentes."""
    ids = [generate_snowflake_id() for _ in range(50)]
    suffixes = [int(s.removeprefix("snowflake_")) for s in ids]
    assert suffixes == sorted(suffixes), "Snowflake ids nao monotonic crescentes"
    assert len(set(suffixes)) == 50, "Duplicatas no batch sequencial"


def test_generate_snowflake_id_unique_across_threads():
    """Mesmo com varias threads gerando concorrentemente, ids sao unicos."""
    results: list[str] = []
    lock = threading.Lock()

    def worker():
        local: list[str] = []
        for _ in range(20):
            local.append(generate_snowflake_id())
        with lock:
            results.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 100
    assert len(set(results)) == 100, "Snowflake colision entre threads"


# ---------------------------------------------------------------------------
# Hash user id
# ---------------------------------------------------------------------------


def test_hash_user_id_never_returns_raw_phone():
    raw = "5521989091014"
    hashed = hash_user_id(raw)
    assert hashed != raw
    assert raw not in hashed
    assert "89091014" not in hashed


def test_hash_user_id_is_deterministic():
    assert hash_user_id("5521989091014") == hash_user_id("5521989091014")
    # Mesmos ultimos 8 digitos => mesmo hash
    assert hash_user_id("5521989091014") == hash_user_id("9989091014")


def test_hash_user_id_different_inputs_yield_different_hashes():
    assert hash_user_id("5521989091014") != hash_user_id("5521000000000")


def test_hash_user_id_handles_empty_and_none():
    assert hash_user_id(None) == "unknown"
    assert hash_user_id("") == "unknown"


def test_hash_user_id_handles_non_string():
    assert hash_user_id(12345) == "unknown"  # type: ignore[arg-type]


def test_hash_user_id_prefix_is_short_hex():
    hashed = hash_user_id("5521989091014")
    assert hashed.startswith("u_")
    suffix = hashed.removeprefix("u_")
    assert len(suffix) == 16
    assert all(c in "0123456789abcdef" for c in suffix)


# ---------------------------------------------------------------------------
# PII redaction
# ---------------------------------------------------------------------------


def test_redact_pii_cpf_with_punctuation():
    out = redact_pii("Meu CPF e 123.456.789-00")
    assert "[REDACTED_CPF]" in out
    assert "123.456.789-00" not in out


def test_redact_pii_cpf_only_digits():
    out = redact_pii("CPF 12345678900 confirmado")
    # Pode ser capturado por CPF_PATTERN ou phone_e164 (11 digitos).
    # Aceitamos qualquer um -- o importante e nao deixar raw.
    assert "12345678900" not in out


def test_redact_pii_cep():
    out = redact_pii("Endereco CEP 20040-020 Centro")
    assert "[REDACTED_CEP]" in out
    assert "20040-020" not in out
    assert "20040020" not in out


def test_redact_pii_phone_e164():
    out = redact_pii("Telefone +5521989091014 chamado")
    assert "[REDACTED_PHONE]" in out
    assert "5521989091014" not in out


def test_redact_pii_email():
    out = redact_pii("Contato joao.silva+test@example.com com bot")
    assert "[REDACTED_EMAIL]" in out
    assert "joao.silva+test@example.com" not in out


def test_redact_pii_preserves_non_pii_text():
    out = redact_pii("Endereco: Rua Guilhermina Guinle, 170 - Botafogo")
    assert "Botafogo" in out
    assert "Guilhermina" in out


def test_redact_pii_empty_or_none():
    assert redact_pii("") == ""
    assert redact_pii(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Decorator: sync + async + success + failure
# ---------------------------------------------------------------------------


def test_audit_log_decorator_sync_records_success(captured_audit_records):
    @audit_log(action_type="test_sync_action", sensitivity="low")
    def add(a: int, b: int) -> dict:
        return {"sum": a + b}

    result = add(2, 3)
    assert result == {"sum": 5}
    assert len(captured_audit_records) == 1
    rec = captured_audit_records[0]
    assert rec["action_type"] == "test_sync_action"
    assert rec["tool_name"] == "add"
    assert rec["success"] is True
    assert rec["sensitivity"] == "low"
    assert "snowflake_id" in rec
    assert "timestamp_utc" in rec


@pytest.mark.asyncio
async def test_audit_log_decorator_async_records_success(captured_audit_records):
    @audit_log(action_type="test_async_action", sensitivity="high")
    async def async_op(user_id: str, payload: dict) -> dict:
        await asyncio.sleep(0)
        return {"status": "ok"}

    result = await async_op(user_id="5521989091014", payload={"foo": "bar"})
    assert result == {"status": "ok"}
    assert len(captured_audit_records) == 1
    rec = captured_audit_records[0]
    assert rec["action_type"] == "test_async_action"
    assert rec["success"] is True
    assert rec["sensitivity"] == "high"


@pytest.mark.asyncio
async def test_audit_log_decorator_records_failure_and_reraises(captured_audit_records):
    @audit_log(action_type="failing_action", sensitivity="medium")
    async def fail() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await fail()
    assert len(captured_audit_records) == 1
    rec = captured_audit_records[0]
    assert rec["success"] is False
    # Output_summary deve carregar o erro
    assert "boom" in rec["output_summary"] or "ValueError" in rec["output_summary"]


@pytest.mark.asyncio
async def test_audit_log_decorator_extracts_user_id_and_hashes_it(
    captured_audit_records,
):
    """O user_id passado como kwarg deve ser hashed antes de logar --
    NUNCA emitido raw. (Default conservador exige kwarg explicito; positional
    requer `extract_user_id` customizado -- ver Codex review P2.)
    """

    @audit_log(action_type="user_action", sensitivity="medium")
    async def op_with_user(user_id: str, message: str) -> dict:
        return {"echo": message}

    raw_phone = "5521989091014"
    await op_with_user(user_id=raw_phone, message="ola")
    rec = captured_audit_records[0]
    assert rec["user_hash"] != raw_phone
    assert raw_phone not in rec["user_hash"]
    assert rec["user_hash"].startswith("u_")
    # Verifica tambem que o input_summary tem o phone REDACTED
    assert raw_phone not in rec["input_summary"]


@pytest.mark.asyncio
async def test_audit_log_decorator_default_does_not_infer_positional_user_id(
    captured_audit_records,
):
    """Codex review P2 (gpt-5.3-codex-spark): default conservador NAO
    deve adivinhar user_id a partir de positional args. Pega `unknown`
    em vez de hash bogus -- melhor ser explicito que enganador."""

    @audit_log(action_type="builder_action", sensitivity="low")
    def builder(type_: str, url: str) -> dict:
        return {"ok": True}

    # type="image" e url="https://..." -- nenhum eh user_id.
    builder("image", "https://example.com/x.jpg")
    rec = captured_audit_records[0]
    # Sem extract_user_id customizado + sem kwargs nominais => unknown.
    assert rec["user_hash"] == "unknown"


@pytest.mark.asyncio
async def test_audit_log_decorator_positional_user_id_via_explicit_extractor(
    captured_audit_records,
):
    """Pra tools que recebem user_id como primeiro positional, o caller
    DEVE passar `extract_user_id` customizado. Demonstra esse padrao."""

    @audit_log(
        action_type="explicit_extract",
        sensitivity="medium",
        extract_user_id=lambda args, kwargs: (
            kwargs.get("user_id") or (args[0] if args else None)
        ),
    )
    async def op(user_id: str, payload: dict) -> dict:
        return {"ok": True}

    await op("5521989091014", {"data": "x"})
    rec = captured_audit_records[0]
    assert rec["user_hash"].startswith("u_")
    assert rec["user_hash"] != "unknown"


@pytest.mark.asyncio
async def test_audit_log_decorator_truncates_long_input_and_output(
    captured_audit_records,
):
    @audit_log(action_type="big_payload", sensitivity="low")
    async def op(payload: str) -> str:
        return payload * 10

    huge_input = "x" * 2000
    result = await op(huge_input)
    assert len(result) == 20000
    rec = captured_audit_records[0]
    assert len(rec["input_summary"]) <= _DEFAULT_SUMMARY_MAX
    assert len(rec["output_summary"]) <= _DEFAULT_SUMMARY_MAX
    assert rec["input_summary"].endswith("...")
    assert rec["output_summary"].endswith("...")


def test_audit_log_decorator_redacts_pii_in_input_summary(captured_audit_records):
    """Mesmo que o caller esqueca de redact, o decorator aplica defensiva."""

    @audit_log(action_type="pii_test", sensitivity="medium")
    def op(description: str) -> dict:
        return {"ok": True}

    op("Meu CPF e 123.456.789-00 e CEP 20040-020")
    rec = captured_audit_records[0]
    assert "123.456.789-00" not in rec["input_summary"]
    assert "20040-020" not in rec["input_summary"]
    assert "[REDACTED_CPF]" in rec["input_summary"]
    assert "[REDACTED_CEP]" in rec["input_summary"]


def test_audit_log_decorator_extract_user_id_callable_override(captured_audit_records):
    """Usuario pode customizar a extracao do user_id."""

    @audit_log(
        action_type="custom_extract",
        sensitivity="medium",
        extract_user_id=lambda args, kwargs: kwargs.get("phone"),
    )
    def op(message: str, phone: str = "") -> dict:
        return {"ok": True}

    op("ola", phone="5521989091014")
    rec = captured_audit_records[0]
    assert rec["user_hash"].startswith("u_")
    assert rec["user_hash"] != "unknown"


# ---------------------------------------------------------------------------
# record_action low-level
# ---------------------------------------------------------------------------


def test_record_action_returns_canonical_fields(captured_audit_records):
    rec = record_action(
        action_type="manual_action",
        tool_name="some_tool",
        user_hash="u_abcdef0123456789",
        input_summary="payload",
        output_summary="result",
        success=True,
        sensitivity="high",
    )
    expected_keys = {
        "snowflake_id",
        "timestamp_utc",
        "action_type",
        "tool_name",
        "user_hash",
        "input_summary",
        "output_summary",
        "success",
        "sensitivity",
        "trace_id",
    }
    assert expected_keys.issubset(rec.keys())
    assert rec["action_type"] == "manual_action"
    assert rec["sensitivity"] == "high"
    assert rec["success"] is True
    assert len(captured_audit_records) == 1


def test_record_action_timestamp_is_iso_utc(captured_audit_records):
    rec = record_action(
        action_type="iso_test",
        tool_name="t",
        user_hash="u_xx",
        input_summary="",
        output_summary="",
        success=True,
        sensitivity="low",
    )
    ts = rec["timestamp_utc"]
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", ts), (
        f"timestamp nao-ISO: {ts}"
    )
    assert "+00:00" in ts or ts.endswith("Z"), f"timestamp nao UTC: {ts}"


# ---------------------------------------------------------------------------
# Codex review P2 follow-ups (gpt-5.3-codex-spark)
# ---------------------------------------------------------------------------


# Fix 1: _MACHINE_ID safe parse


def test_parse_machine_id_handles_none():
    assert _parse_machine_id(None) == 0


def test_parse_machine_id_handles_empty_string():
    assert _parse_machine_id("") == 0
    assert _parse_machine_id("   ") == 0


def test_parse_machine_id_handles_invalid_value():
    assert _parse_machine_id("foo") == 0
    assert _parse_machine_id("12.5") == 0  # nao int puro


def test_parse_machine_id_valid_in_range():
    assert _parse_machine_id("42") == 42
    assert _parse_machine_id("1023") == 1023  # max 10 bits
    assert _parse_machine_id("0") == 0


def test_parse_machine_id_rejects_out_of_range():
    """Codex review P3: valores fora de 0-1023 sao REJEITADOS pra 0,
    nao bitmasked (que silenciosamente mapearia -1 -> 1023)."""
    assert _parse_machine_id("1024") == 0
    assert _parse_machine_id("2000") == 0
    assert _parse_machine_id("-1") == 0
    assert _parse_machine_id("-1000") == 0


def test_parse_machine_id_accepts_boundary_values():
    assert _parse_machine_id("0") == 0
    assert _parse_machine_id("1023") == 1023


# Codex review P3 (gpt-5.3-codex-spark): record_action re-hashia raw input


def test_record_action_rehashes_raw_phone_in_user_hash_param(captured_audit_records):
    """Caller passou telefone raw como user_hash (engano comum). O record
    emitido NUNCA deve carregar PII raw."""
    raw_phone = "5521989091014"
    rec = record_action(
        action_type="manual_raw",
        tool_name="t",
        user_hash=raw_phone,  # caller errado passou raw em vez de hash
        input_summary="",
        output_summary="",
        success=True,
        sensitivity="medium",
    )
    assert rec["user_hash"] != raw_phone
    assert raw_phone not in rec["user_hash"]
    assert rec["user_hash"].startswith("u_")
    # No record capturado idem
    assert captured_audit_records[0]["user_hash"].startswith("u_")
    assert raw_phone not in captured_audit_records[0]["user_hash"]


def test_record_action_preserves_already_hashed_value(captured_audit_records):
    """Se ja vier no formato `u_<hex>`, nao re-hashia (idempotencia)."""
    already_hashed = "u_abcdef0123456789"
    rec = record_action(
        action_type="manual_idempotent",
        tool_name="t",
        user_hash=already_hashed,
        input_summary="",
        output_summary="",
        success=True,
        sensitivity="low",
    )
    assert rec["user_hash"] == already_hashed


def test_record_action_preserves_unknown_literal(captured_audit_records):
    """`unknown` literal nao deve ser re-hashado em outro valor."""
    rec = record_action(
        action_type="manual_unknown",
        tool_name="t",
        user_hash="unknown",
        input_summary="",
        output_summary="",
        success=True,
        sensitivity="low",
    )
    assert rec["user_hash"] == "unknown"


# Fix 2: success_predicate / _default_success_from_result


def test_default_success_predicate_true_for_non_dict():
    assert _default_success_from_result("ok") is True
    assert _default_success_from_result([1, 2, 3]) is True
    assert _default_success_from_result(None) is True
    assert _default_success_from_result(42) is True


def test_default_success_predicate_true_for_clean_dict():
    assert _default_success_from_result({"ok": True}) is True
    assert _default_success_from_result({"protocol_id": "12345"}) is True


def test_default_success_predicate_false_for_success_false():
    assert _default_success_from_result({"success": False, "error": "fail"}) is False


def test_default_success_predicate_false_for_status_error():
    assert _default_success_from_result({"status": "error", "msg": "bad"}) is False
    assert _default_success_from_result({"status": "rejected"}) is False
    assert _default_success_from_result({"status": "failed"}) is False


def test_default_success_predicate_false_for_error_key():
    assert _default_success_from_result({"error": "boom"}) is False


def test_default_success_predicate_false_for_api_resposta_sucesso_false():
    assert _default_success_from_result({"api_resposta_sucesso": False}) is False


def test_audit_log_decorator_infers_failure_from_payload(captured_audit_records):
    """Tools que retornam {"status": "error"} ou {"success": False} sem
    levantar excecao devem ser logadas com success=False."""

    @audit_log(action_type="business_fail", sensitivity="low")
    def build_envelope() -> dict:
        return {"status": "error", "error": "type invalido"}

    build_envelope()
    rec = captured_audit_records[0]
    assert rec["success"] is False


def test_audit_log_decorator_custom_success_predicate(captured_audit_records):
    """Caller pode override o predicate."""

    @audit_log(
        action_type="custom_pred",
        sensitivity="low",
        success_predicate=lambda result: result.get("code") == 0,
    )
    def op() -> dict:
        return {"code": 0, "status": "error"}  # status=error mas code=0 = sucesso

    op()
    rec = captured_audit_records[0]
    assert rec["success"] is True


def test_audit_log_decorator_predicate_off_with_lambda_true(captured_audit_records):
    """`lambda _: True` desliga inspecao default — util pra tools com
    retorno opaco."""

    @audit_log(
        action_type="opaque",
        sensitivity="low",
        success_predicate=lambda _: True,
    )
    def op() -> dict:
        return {"status": "error"}  # default flagaria como fail

    op()
    rec = captured_audit_records[0]
    assert rec["success"] is True


# Fix 3: Snowflake monotonicity under clock rollback


def test_snowflake_burst_does_not_deadlock_on_counter_overflow(monkeypatch):
    """Codex review P1 (gpt-5.3-codex-spark): pinou time pra forcar
    overflow do counter (4096 ids/ms) -- garante que o branch de
    overflow nao loopa infinitamente e que ids permanecem monotonic."""
    import time as _real_time
    import src.observability.audit_log as audit_mod

    monkeypatch.setattr(_real_time, "time", lambda: 12345.000)  # tempo fixo
    audit_mod._snowflake_last_ms = 0
    audit_mod._snowflake_counter = 0

    # Burst > 4096 (1 slot completo + overflow algumas vezes)
    ids = [generate_snowflake_id() for _ in range(5000)]
    suffixes = [int(s.removeprefix("snowflake_")) for s in ids]
    assert suffixes == sorted(suffixes), "Burst quebrou monotonicidade"
    assert len(set(suffixes)) == 5000, "Burst gerou duplicatas"


def test_snowflake_monotonic_under_clock_rollback(monkeypatch):
    """Mesmo se `time.time()` andar pra TRAS (NTP step), ids permanecem
    monotonic crescentes."""
    import time as _real_time

    import src.observability.audit_log as audit_mod

    # Sequencia: 1000ms, 2000ms, 1500ms (rollback!), 2500ms.
    # Cada chamada de generate_snowflake_id() pode chamar time.time() mais
    # de uma vez no caminho de overflow; saturamos no ultimo valor.
    fake_times = [1.000, 2.000, 1.500, 2.500]
    counter = {"i": 0}

    def fake_time():
        idx = min(counter["i"], len(fake_times) - 1)
        counter["i"] += 1
        return fake_times[idx]

    # `audit_log` faz `import time` e chama `time.time()`. Pra interceptar
    # so o uso dentro do modulo, patcheamos diretamente o objeto `time`
    # importado por audit_log (sem mexer no module global do CPython).
    monkeypatch.setattr(_real_time, "time", fake_time)

    # Reseta state interno pra nao herdar de testes anteriores.
    audit_mod._snowflake_last_ms = 0
    audit_mod._snowflake_counter = 0

    ids = [generate_snowflake_id() for _ in range(4)]
    suffixes = [int(s.removeprefix("snowflake_")) for s in ids]
    assert suffixes == sorted(suffixes), (
        f"Snowflake quebrou monotonicidade sob clock rollback: {suffixes}"
    )
    assert len(set(suffixes)) == 4, "Duplicatas sob clock rollback"
