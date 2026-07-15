from jsonschema import validate

from src.app import (
    MULTI_STEP_SERVICE_OUTPUT_SCHEMA,
    get_multi_step_service_tool_options,
)


def test_multi_step_service_exposes_salesforce_outputs() -> None:
    properties = MULTI_STEP_SERVICE_OUTPUT_SCHEMA["properties"]

    assert properties["description"] == {"type": "string"}
    assert "error_message" in properties
    assert "payload_schema" in properties
    assert "data" in properties
    assert "required" not in MULTI_STEP_SERVICE_OUTPUT_SCHEMA
    assert MULTI_STEP_SERVICE_OUTPUT_SCHEMA["additionalProperties"] is True


def test_multi_step_service_schema_preserves_alternative_responses() -> None:
    validate(
        {"description": "Informe o ano", "data": {}},
        MULTI_STEP_SERVICE_OUTPUT_SCHEMA,
    )


def test_multi_step_service_keeps_compatibility_with_legacy_fastmcp() -> None:
    class LegacyFastMCP:
        def tool(self, name=None, *, description=None):
            pass

    assert get_multi_step_service_tool_options(LegacyFastMCP) == {}
    validate(
        {"status": "interactive_sent", "next_step": "await_user_selection"},
        MULTI_STEP_SERVICE_OUTPUT_SCHEMA,
    )
