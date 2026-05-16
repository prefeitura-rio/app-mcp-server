"""Unit tests pros helpers de WhatsApp interactive (ADR-022 + ADR-024)."""

from src.tools.whatsapp_interactive import (
    build_buttons_envelope,
    build_flow_envelope,
    build_list_envelope,
)


# ===================== Flow =====================


def test_flow_happy_path():
    env = build_flow_envelope(
        flow_id="4141008006029185",
        body="Vou abrir um chamado pra você.",
        flow_token="uuid-test-1",
        cta="Preencher",
        header="Luminária",
        footer="Prefeitura do Rio",
    )
    assert env["status"] == "ok"
    assert env["type"] == "interactive"
    iv = env["interactive"]
    assert iv["type"] == "flow"
    assert iv["body"]["text"].startswith("Vou abrir")
    assert iv["header"]["text"] == "Luminária"
    assert iv["footer"]["text"] == "Prefeitura do Rio"
    params = iv["action"]["parameters"]
    assert params["flow_id"] == "4141008006029185"
    assert params["flow_token"] == "uuid-test-1"
    assert params["flow_cta"] == "Preencher"
    assert params["flow_action"] == "navigate"
    assert params["flow_message_version"] == "3"


def test_flow_minimal_required_only():
    env = build_flow_envelope(flow_id="123", body="Texto.", flow_token="tok")
    assert env["status"] == "ok"
    iv = env["interactive"]
    assert "header" not in iv
    assert "footer" not in iv
    assert iv["action"]["parameters"]["flow_cta"] == "Abrir formulário"


def test_flow_missing_flow_id():
    env = build_flow_envelope(flow_id="", body="x", flow_token="t")
    assert env["status"] == "error"
    assert "flow_id" in env["error"]


def test_flow_missing_body():
    env = build_flow_envelope(flow_id="f", body="", flow_token="t")
    assert env["status"] == "error"
    assert "body" in env["error"]


def test_flow_missing_token():
    env = build_flow_envelope(flow_id="f", body="x", flow_token="")
    assert env["status"] == "error"
    assert "flow_token" in env["error"]


def test_flow_body_too_long():
    env = build_flow_envelope(flow_id="f", body="a" * 1100, flow_token="t")
    assert env["status"] == "error"
    assert "1024" in env["error"]


def test_flow_cta_too_long():
    env = build_flow_envelope(flow_id="f", body="x", flow_token="t", cta="a" * 25)
    assert env["status"] == "error"


def test_flow_invalid_action():
    env = build_flow_envelope(flow_id="f", body="x", flow_token="t", flow_action="hack")
    assert env["status"] == "error"
    assert "flow_action" in env["error"]


def test_flow_custom_action_payload():
    env = build_flow_envelope(
        flow_id="f",
        body="x",
        flow_token="t",
        flow_action_payload={"screen": "DEFECT_TYPE_SCREEN", "data": {"prefill": "x"}},
    )
    assert env["status"] == "ok"
    payload = env["interactive"]["action"]["parameters"]["flow_action_payload"]
    assert payload["screen"] == "DEFECT_TYPE_SCREEN"
    assert payload["data"]["prefill"] == "x"


# ===================== Buttons =====================


def test_buttons_happy_path():
    env = build_buttons_envelope(
        body="Como posso ajudar?",
        buttons=[
            {"id": "agendar", "title": "Agendar"},
            {"id": "consultar", "title": "Consultar"},
            {"id": "ajuda", "title": "Ajuda"},
        ],
    )
    assert env["status"] == "ok"
    iv = env["interactive"]
    assert iv["type"] == "button"
    btns = iv["action"]["buttons"]
    assert len(btns) == 3
    assert btns[0]["reply"]["id"] == "agendar"
    assert btns[0]["reply"]["title"] == "Agendar"


def test_buttons_empty_returns_error():
    env = build_buttons_envelope(body="x", buttons=[])
    assert env["status"] == "error"


def test_buttons_too_many_returns_error():
    env = build_buttons_envelope(
        body="x",
        buttons=[{"id": f"b{i}", "title": f"T{i}"} for i in range(4)],
    )
    assert env["status"] == "error"
    assert "máx 3" in env["error"] or "max 3" in env["error"].lower()


def test_buttons_duplicate_id_returns_error():
    env = build_buttons_envelope(
        body="x",
        buttons=[
            {"id": "dup", "title": "A"},
            {"id": "dup", "title": "B"},
        ],
    )
    assert env["status"] == "error"
    assert "duplicado" in env["error"]


def test_buttons_missing_title():
    env = build_buttons_envelope(body="x", buttons=[{"id": "b", "title": ""}])
    assert env["status"] == "error"


def test_buttons_title_too_long():
    env = build_buttons_envelope(body="x", buttons=[{"id": "b", "title": "A" * 25}])
    assert env["status"] == "error"


# ===================== List =====================


def test_list_happy_path():
    env = build_list_envelope(
        body="Escolha um serviço:",
        sections=[
            {
                "title": "Iluminação",
                "rows": [
                    {"id": "luminaria_quebrada", "title": "Luminária quebrada"},
                    {"id": "poste_caido", "title": "Poste caído"},
                ],
            },
            {
                "title": "Limpeza",
                "rows": [{"id": "coleta_irregular", "title": "Coleta de lixo"}],
            },
        ],
    )
    assert env["status"] == "ok"
    iv = env["interactive"]
    assert iv["type"] == "list"
    assert len(iv["action"]["sections"]) == 2
    assert len(iv["action"]["sections"][0]["rows"]) == 2


def test_list_with_descriptions():
    env = build_list_envelope(
        body="x",
        sections=[
            {
                "title": "Sec",
                "rows": [
                    {
                        "id": "r1",
                        "title": "Row 1",
                        "description": "Detalhe do row 1",
                    }
                ],
            }
        ],
    )
    assert env["status"] == "ok"
    row = env["interactive"]["action"]["sections"][0]["rows"][0]
    assert row["description"] == "Detalhe do row 1"


def test_list_total_rows_exceeded():
    sections = [
        {
            "title": "S",
            "rows": [{"id": f"r{i}", "title": f"T{i}"} for i in range(11)],
        }
    ]
    env = build_list_envelope(body="x", sections=sections)
    assert env["status"] == "error"
    assert "10" in env["error"]


def test_list_section_title_too_long():
    env = build_list_envelope(
        body="x",
        sections=[{"title": "A" * 30, "rows": [{"id": "r", "title": "T"}]}],
    )
    assert env["status"] == "error"


def test_list_duplicate_row_id_returns_error():
    env = build_list_envelope(
        body="x",
        sections=[
            {"title": "Sec1", "rows": [{"id": "dup", "title": "A"}]},
            {"title": "Sec2", "rows": [{"id": "dup", "title": "B"}]},
        ],
    )
    assert env["status"] == "error"
    assert "duplicado" in env["error"]


def test_list_button_label_too_long():
    env = build_list_envelope(
        body="x",
        sections=[{"title": "S", "rows": [{"id": "r", "title": "T"}]}],
        button_label="A" * 25,
    )
    assert env["status"] == "error"


def test_list_empty_sections():
    env = build_list_envelope(body="x", sections=[])
    assert env["status"] == "error"
