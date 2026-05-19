#!/usr/bin/env python3
"""Valida media-types.yaml contra media-types.schema.json.

Roda em CI (`.github/workflows/validate-media-types.yml`) e pre-commit
local. Falha se:
  - YAML não parse
  - schema JSON Schema 2020-12 inválido
  - registry não conforma com schema
  - inconsistência semântica (e.g. tool name referenciada não existe em src/app.py)

Uso:
    python tools/validate_media_types_registry.py
    python tools/validate_media_types_registry.py --strict   # incluir checks semânticos cross-repo
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "media-types.yaml"
SCHEMA_PATH = REPO_ROOT / "media-types.schema.json"
APP_PY_PATH = REPO_ROOT / "src" / "app.py"


def _load_yaml(path: Path) -> dict:
    try:
        import yaml  # type: ignore
    except ImportError:
        print(
            "ERROR: PyYAML not installed. Run: uv pip install pyyaml", file=sys.stderr
        )
        sys.exit(2)
    with open(path) as f:
        return yaml.safe_load(f)


def _load_json(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def _validate_schema(registry: dict, schema: dict) -> list[str]:
    try:
        import jsonschema  # type: ignore
    except ImportError:
        print(
            "ERROR: jsonschema not installed. Run: uv pip install jsonschema",
            file=sys.stderr,
        )
        sys.exit(2)
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema)
    errors: list[str] = []
    for err in validator.iter_errors(registry):
        path = ".".join(str(p) for p in err.absolute_path) or "<root>"
        errors.append(f"{path}: {err.message}")
    return errors


def _validate_base64_requires_mime(registry: dict) -> list[str]:
    """Para tipos com builder_tool=send_whatsapp_media e required_fields contendo
    "url|base64", `mime_type` deve estar em optional_fields ou required_fields —
    senão consumer pode gerar payload base64 sem MIME que MCP rejeita em runtime.
    Esta regra não é representável em pure JSON Schema (conditional cross-field).
    """
    errors: list[str] = []
    for type_name, spec in registry.get("types", {}).items():
        out = spec.get("outbound", {})
        if out.get("builder_tool") != "send_whatsapp_media":
            continue
        required = out.get("required_fields", [])
        optional = out.get("optional_fields", [])
        has_upload = any("base64" in f for f in required)
        if has_upload and "mime_type" not in (required + optional):
            errors.append(
                f"types.{type_name}.outbound: tipo aceita base64 upload mas "
                f"`mime_type` não está em required_fields nem optional_fields. "
                f"build_whatsapp_media_envelope exige mime_type pra base64."
            )
    return errors


def _validate_tool_references(registry: dict, strict: bool) -> list[str]:
    """Checa que tools referenciadas no registry existem em src/app.py."""
    if not strict:
        return []
    if not APP_PY_PATH.exists():
        return [f"strict mode: src/app.py ausente em {APP_PY_PATH}"]
    app_src = APP_PY_PATH.read_text()
    registered_tools = set(
        re.findall(r'@conditional_mcp_tool\(\s*"([a-z_]+)"', app_src)
    )
    errors: list[str] = []
    for type_name, spec in registry.get("types", {}).items():
        for direction in ("inbound", "outbound"):
            sub = spec.get(direction, {})
            if direction not in spec:  # field não presente — direção não suportada
                continue
            tool_field = "analyzer_tool" if direction == "inbound" else "builder_tool"
            if tool_field not in sub:
                continue
            tool = sub[tool_field]
            # null é forma documentada de dizer "sem MCP tool" (e.g. text outbound vai
            # via Mule direto, sem analyzer). Empty string NÃO é equivalente — schema
            # aceita string mas consumer ficaria sem tool callable.
            if tool is None:
                continue
            if not isinstance(tool, str) or tool == "":
                errors.append(
                    f"types.{type_name}.{direction}.{tool_field}={tool!r} "
                    f"é string vazia — use `null` se direção não tem tool MCP."
                )
                continue
            if tool not in registered_tools:
                errors.append(
                    f"types.{type_name}.{direction}.{tool_field}={tool!r} "
                    f"não está registrada em src/app.py via @conditional_mcp_tool"
                )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Inclui checks semânticos cross-repo (tool names existem em src/app.py).",
    )
    args = parser.parse_args()

    print(f"Loading registry: {REGISTRY_PATH}")
    registry = _load_yaml(REGISTRY_PATH)
    print(f"Loading schema:   {SCHEMA_PATH}")
    schema = _load_json(SCHEMA_PATH)

    schema_errors = _validate_schema(registry, schema)
    # Semantic checks assumem mapping shape — pulam se schema falhou pra evitar
    # AttributeError com tracebacks confusos. Schema errors são suficientes pro
    # operador corrigir o YAML antes de revalidar.
    if schema_errors:
        all_errors = schema_errors
    else:
        base64_errors = _validate_base64_requires_mime(registry)
        semantic_errors = _validate_tool_references(registry, args.strict)
        all_errors = schema_errors + base64_errors + semantic_errors

    if all_errors:
        print(f"\n❌ {len(all_errors)} error(s):")
        for e in all_errors:
            print(f"  - {e}")
        return 1

    n_types = len(registry.get("types", {}))
    print(
        f"\n✅ Registry valido: protocol_version={registry.get('protocol_version')}, {n_types} types definidos"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
