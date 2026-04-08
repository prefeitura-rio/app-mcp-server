#!/usr/bin/env python3
import json
import os
import sys
import urllib.error
import urllib.request


BASE_URL = os.environ.get("PREVIEW_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
VALID_TOKENS = os.environ.get("VALID_TOKENS", "")
CONSULTA_TIPO = os.environ.get("PREVIEW_CONSULTA_TIPO", "")
CONSULTA_VALOR = os.environ.get("PREVIEW_CONSULTA_VALOR", "")
ANO_AUTO = os.environ.get("PREVIEW_CONSULTA_ANO_AUTO_INFRACAO", "")


def fail(message: str, details=None) -> None:
    print(f"FAIL: {message}")
    if details is not None:
        if isinstance(details, (dict, list)):
            print(json.dumps(details, ensure_ascii=True, indent=2))
        else:
            print(details)
    sys.exit(1)


def info(message: str) -> None:
    print(f"- {message}")


def get_auth_token() -> str:
    if VALID_TOKENS:
        for token in VALID_TOKENS.split(","):
            token = token.strip()
            if token:
                return token

    return ""


def request_json(path: str, payload=None, token: str | None = None):
    url = f"{BASE_URL}{path}"
    body = json.dumps(payload or {}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return response.status, raw, parse_json(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, raw, parse_json(raw)


def request_text(path: str):
    url = f"{BASE_URL}{path}"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read().decode("utf-8")
            return response.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, raw


def parse_json(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def require_status(actual: int, expected: int, context: str, body) -> None:
    if actual != expected:
        fail(f"{context}: expected HTTP {expected}, got {actual}", body)


def require_json_object(payload, context: str):
    if not isinstance(payload, dict):
        fail(f"{context}: expected JSON object response", payload)


def run_health_check() -> None:
    info("Checking /health")
    status, raw = request_text("/health")
    require_status(status, 200, "health", raw)
    if raw.strip().upper() != "OK":
        fail("health: unexpected body", raw)


def require_authenticated_env() -> None:
    missing = []
    if not get_auth_token():
        missing.append("VALID_TOKENS")
    if not CONSULTA_TIPO:
        missing.append("PREVIEW_CONSULTA_TIPO")
    if not CONSULTA_VALOR:
        missing.append("PREVIEW_CONSULTA_VALOR")

    if missing:
        fail("Missing staging E2E configuration", {"missing_env": missing})


def build_consulta_payload(valid: bool):
    if valid:
        payload = {
            "consulta_debitos": CONSULTA_TIPO,
            CONSULTA_TIPO: CONSULTA_VALOR,
        }
        if CONSULTA_TIPO == "numeroAutoInfracao" and ANO_AUTO:
            payload["anoAutoInfracao"] = ANO_AUTO
        return payload

    payload = {"consulta_debitos": CONSULTA_TIPO, CONSULTA_TIPO: "sem-digitos"}
    if CONSULTA_TIPO == "numeroAutoInfracao":
        payload["anoAutoInfracao"] = "ano-invalido"
    return payload


def run_consulta_happy_path():
    info("Running authenticated consulta_debitos happy path")
    auth_token = get_auth_token()
    status, raw, parsed = request_json(
        "/consulta_debitos",
        payload=build_consulta_payload(valid=True),
        token=auth_token,
    )
    require_status(status, 200, "consulta_debitos happy path", raw)
    require_json_object(parsed, "consulta_debitos happy path")

    if parsed.get("api_resposta_sucesso") is not True:
        fail("consulta_debitos happy path: expected api_resposta_sucesso=true", parsed)

    required_keys = (
        "dicionario_itens",
        "total_itens_pagamento",
        "debitos_msg",
        "mensagem_divida_contribuinte",
    )
    for key in required_keys:
        if key not in parsed:
            fail(f"consulta_debitos happy path: missing key '{key}'", parsed)

    return parsed


def run_consulta_invalid_input_check() -> None:
    info("Running authenticated consulta_debitos invalid-input path")
    auth_token = get_auth_token()
    status, raw, parsed = request_json(
        "/consulta_debitos",
        payload=build_consulta_payload(valid=False),
        token=auth_token,
    )
    require_status(status, 200, "consulta_debitos invalid input", raw)
    require_json_object(parsed, "consulta_debitos invalid input")

    if parsed.get("api_resposta_sucesso") is not False:
        fail(
            "consulta_debitos invalid input: expected api_resposta_sucesso=false",
            parsed,
        )
    if "api_descricao_erro" not in parsed:
        fail("consulta_debitos invalid input: missing api_descricao_erro", parsed)


def find_item_index(consulta_payload, values):
    item_map = consulta_payload.get("dicionario_itens", {})
    for index, value in item_map.items():
        if value in values:
            return str(index)
    return None


def run_emitir_guia_minimal_payload_checks() -> None:
    info("Checking guia endpoints with minimal payload")
    auth_token = get_auth_token()
    for route in ("/emitir_guia", "/emitir_guia_regularizacao"):
        status, raw, parsed = request_json(route, payload={}, token=auth_token)
        require_status(status, 200, f"{route} invalid payload", raw)
        require_json_object(parsed, f"{route} minimal payload")
        if "api_resposta_sucesso" not in parsed:
            fail(f"{route} minimal payload: missing api_resposta_sucesso", parsed)


def run_emitir_guia_happy_paths(consulta_payload) -> None:
    auth_token = get_auth_token()
    common_payload = {
        "dicionario_itens": json.dumps(
            consulta_payload.get("dicionario_itens", {}), ensure_ascii=True
        ),
        "lista_cdas": json.dumps(
            consulta_payload.get("lista_cdas", []), ensure_ascii=True
        ),
        "lista_efs": json.dumps(
            consulta_payload.get("lista_efs", []), ensure_ascii=True
        ),
        "lista_guias": json.dumps(
            consulta_payload.get("lista_guias", []), ensure_ascii=True
        ),
    }

    vista_values = set(consulta_payload.get("lista_cdas", [])) | set(
        consulta_payload.get("lista_efs", [])
    )
    vista_index = find_item_index(consulta_payload, vista_values)
    if vista_index:
        info("Running authenticated emitir_guia happy path")
        payload = common_payload | {"apenas_um_item": vista_index}
        status, raw, parsed = request_json(
            "/emitir_guia", payload=payload, token=auth_token
        )
        require_status(status, 200, "emitir_guia happy path", raw)
        require_json_object(parsed, "emitir_guia happy path")
        if parsed.get("api_resposta_sucesso") is not True:
            fail("emitir_guia happy path: expected api_resposta_sucesso=true", parsed)
        for key in ("codigo_de_barras", "link"):
            if key not in parsed:
                fail(f"emitir_guia happy path: missing key '{key}'", parsed)
    else:
        info(
            "Skipping emitir_guia happy path because consulta_debitos returned no eligible item"
        )

    regularizacao_values = set(consulta_payload.get("lista_guias", []))
    regularizacao_index = find_item_index(consulta_payload, regularizacao_values)
    if regularizacao_index:
        info("Running authenticated emitir_guia_regularizacao happy path")
        payload = common_payload | {"apenas_um_item": regularizacao_index}
        status, raw, parsed = request_json(
            "/emitir_guia_regularizacao", payload=payload, token=auth_token
        )
        require_status(status, 200, "emitir_guia_regularizacao happy path", raw)
        require_json_object(parsed, "emitir_guia_regularizacao happy path")
        if parsed.get("api_resposta_sucesso") is not True:
            fail(
                "emitir_guia_regularizacao happy path: expected api_resposta_sucesso=true",
                parsed,
            )
        for key in ("codigo_de_barras", "link"):
            if key not in parsed:
                fail(
                    f"emitir_guia_regularizacao happy path: missing key '{key}'", parsed
                )
    else:
        info(
            "Skipping emitir_guia_regularizacao happy path because consulta_debitos returned no eligible item"
        )


def main() -> None:
    print(f"Running preview E2E checks against: {BASE_URL}")
    run_health_check()
    require_authenticated_env()
    consulta_payload = run_consulta_happy_path()
    run_consulta_invalid_input_check()
    run_emitir_guia_minimal_payload_checks()
    run_emitir_guia_happy_paths(consulta_payload)
    print("Preview E2E checks passed.")


if __name__ == "__main__":
    main()
