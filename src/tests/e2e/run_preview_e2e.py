#!/usr/bin/env python3
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request


BASE_URL = os.environ.get("PREVIEW_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
VALID_TOKENS = os.environ.get("VALID_TOKENS", "")
CONSULTA_TIPO = os.environ.get("PREVIEW_CONSULTA_TIPO", "")
CONSULTA_VALOR = os.environ.get("PREVIEW_CONSULTA_VALOR", "")
ANO_AUTO = os.environ.get("PREVIEW_CONSULTA_ANO_AUTO_INFRACAO", "")
AVISTA_PAYLOAD = os.environ.get("PREVIEW_AVISTA_PAYLOAD", "")
REGULARIZACAO_PAYLOAD = os.environ.get("PREVIEW_REGULARIZACAO_PAYLOAD", "")
DEFAULT_POST_TIMEOUT = int(os.environ.get("PREVIEW_E2E_POST_TIMEOUT", "60"))
DEFAULT_GET_TIMEOUT = int(os.environ.get("PREVIEW_E2E_GET_TIMEOUT", "15"))
GUIDE_POST_TIMEOUT = int(os.environ.get("PREVIEW_E2E_GUIDE_TIMEOUT", "90"))
# Retries pros endpoints que dependem de APIs externas (Dívida Ativa).
# Quando upstream retorna api_resposta_sucesso=false transitoriamente,
# retry distingue infra glitch vs regressão real. Não usa exponential
# backoff (delay linear suficiente pra flakiness curta da API externa).
EXTERNAL_API_MAX_ATTEMPTS = int(os.environ.get("PREVIEW_E2E_EXTERNAL_RETRIES", "3"))
EXTERNAL_API_RETRY_DELAY_SECONDS = int(
    os.environ.get("PREVIEW_E2E_EXTERNAL_RETRY_DELAY", "10")
)
# Substrings em `api_descricao_erro` que indicam "fixture stale" (dados
# de teste em Infisical não-aplicáveis hoje, e.g. guia paga, sem débitos)
# em vez de regressão do nosso código. Quando upstream retorna um desses,
# o test SKIPPA em vez de falhar — preview environment está saudável,
# a fixture é que precisa atualizar.
STALE_FIXTURE_HINTS = (
    "não há parcelas em atraso",
    "nao ha parcelas em atraso",
    "não há débitos",
    "nao ha debitos",
    "sem débitos",
    "sem debitos",
    "guia paga",
    "guia ja paga",
    "guia já paga",
)


def is_stale_fixture_error(parsed) -> bool:
    """True se o response upstream indica que a fixture de teste está
    obsoleta (não há trabalho pra fazer pra os dados configurados em
    Infisical). NÃO é regressão do nosso código."""
    if not isinstance(parsed, dict):
        return False
    if parsed.get("api_resposta_sucesso") is True:
        return False
    desc = (parsed.get("api_descricao_erro") or "").lower()
    return any(hint in desc for hint in STALE_FIXTURE_HINTS)


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


def request_json(
    path: str,
    payload=None,
    token: str | None = None,
    timeout: int = DEFAULT_POST_TIMEOUT,
):
    url = f"{BASE_URL}{path}"
    body = json.dumps(payload or {}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return response.status, raw, parse_json(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, raw, parse_json(raw)
    except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
        fail(f"{path}: request timed out or failed to connect", str(exc))


def request_text(path: str, timeout: int = DEFAULT_GET_TIMEOUT):
    url = f"{BASE_URL}{path}"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return response.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, raw
    except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
        fail(f"{path}: request timed out or failed to connect", str(exc))


def parse_json(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def load_json_env(name: str, raw_value: str):
    if not raw_value:
        return None

    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        fail(f"{name}: invalid JSON", str(exc))

    if not isinstance(value, dict):
        fail(f"{name}: expected JSON object", value)

    return value


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


def _call_with_external_retry(label: str, do_call):
    """Invoca `do_call()` até `EXTERNAL_API_MAX_ATTEMPTS`x quando upstream
    retorna `api_resposta_sucesso=false`. Retorna a última tupla
    `(status, raw, parsed)`. Para early se já sucedeu.
    """
    status, raw, parsed = do_call()
    for attempt in range(2, EXTERNAL_API_MAX_ATTEMPTS + 1):
        if isinstance(parsed, dict) and parsed.get("api_resposta_sucesso") is True:
            return status, raw, parsed
        info(
            f"{label}: attempt {attempt - 1} returned api_resposta_sucesso!=true, "
            f"retrying in {EXTERNAL_API_RETRY_DELAY_SECONDS}s"
        )
        time.sleep(EXTERNAL_API_RETRY_DELAY_SECONDS)
        status, raw, parsed = do_call()
    if (
        isinstance(parsed, dict)
        and parsed.get("api_resposta_sucesso") is True
        and EXTERNAL_API_MAX_ATTEMPTS > 1
    ):
        info(f"{label}: succeeded on attempt {EXTERNAL_API_MAX_ATTEMPTS}")
    return status, raw, parsed


def run_consulta_happy_path() -> None:
    info("Running authenticated consulta_debitos happy path")
    auth_token = get_auth_token()

    def do_call():
        return request_json(
            "/consulta_debitos",
            payload=build_consulta_payload(valid=True),
            token=auth_token,
        )

    status, raw, parsed = _call_with_external_retry(
        "consulta_debitos happy path", do_call
    )
    require_status(status, 200, "consulta_debitos happy path", raw)
    require_json_object(parsed, "consulta_debitos happy path")

    if is_stale_fixture_error(parsed):
        info(
            "consulta_debitos happy path: SKIPPED — fixture stale "
            f"(upstream: '{parsed.get('api_descricao_erro')}'). "
            "Update PREVIEW_CONSULTA_VALOR em Infisical."
        )
        return

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


def run_emitir_guia_minimal_payload_checks() -> None:
    info("Checking guia endpoints with minimal payload")
    auth_token = get_auth_token()
    for route in ("/emitir_guia", "/emitir_guia_regularizacao"):
        status, raw, parsed = request_json(route, payload={}, token=auth_token)
        require_status(status, 200, f"{route} invalid payload", raw)
        require_json_object(parsed, f"{route} minimal payload")
        if "api_resposta_sucesso" not in parsed:
            fail(f"{route} minimal payload: missing api_resposta_sucesso", parsed)


def run_emitir_guia_happy_paths() -> None:
    auth_token = get_auth_token()
    avista_payload = load_json_env("PREVIEW_AVISTA_PAYLOAD", AVISTA_PAYLOAD)
    if avista_payload:
        info("Running authenticated emitir_guia happy path")

        def do_avista():
            return request_json(
                "/emitir_guia",
                payload=avista_payload,
                token=auth_token,
                timeout=GUIDE_POST_TIMEOUT,
            )

        status, raw, parsed = _call_with_external_retry(
            "emitir_guia happy path", do_avista
        )
        require_status(status, 200, "emitir_guia happy path", raw)
        require_json_object(parsed, "emitir_guia happy path")
        if is_stale_fixture_error(parsed):
            info(
                "emitir_guia happy path: SKIPPED — fixture stale "
                f"(upstream: '{parsed.get('api_descricao_erro')}'). "
                "Update PREVIEW_AVISTA_PAYLOAD em Infisical."
            )
        elif parsed.get("api_resposta_sucesso") is not True:
            fail("emitir_guia happy path: expected api_resposta_sucesso=true", parsed)
        else:
            for key in ("codigo_de_barras", "link"):
                if key not in parsed:
                    fail(f"emitir_guia happy path: missing key '{key}'", parsed)
    else:
        info(
            "Skipping emitir_guia happy path because PREVIEW_AVISTA_PAYLOAD is not set"
        )

    regularizacao_payload = load_json_env(
        "PREVIEW_REGULARIZACAO_PAYLOAD", REGULARIZACAO_PAYLOAD
    )
    if regularizacao_payload:
        info("Running authenticated emitir_guia_regularizacao happy path")

        def do_regularizacao():
            return request_json(
                "/emitir_guia_regularizacao",
                payload=regularizacao_payload,
                token=auth_token,
                timeout=GUIDE_POST_TIMEOUT,
            )

        status, raw, parsed = _call_with_external_retry(
            "emitir_guia_regularizacao happy path", do_regularizacao
        )
        require_status(status, 200, "emitir_guia_regularizacao happy path", raw)
        require_json_object(parsed, "emitir_guia_regularizacao happy path")
        if is_stale_fixture_error(parsed):
            info(
                "emitir_guia_regularizacao happy path: SKIPPED — fixture stale "
                f"(upstream: '{parsed.get('api_descricao_erro')}'). "
                "Update PREVIEW_REGULARIZACAO_PAYLOAD em Infisical."
            )
        elif parsed.get("api_resposta_sucesso") is not True:
            fail(
                "emitir_guia_regularizacao happy path: expected api_resposta_sucesso=true",
                parsed,
            )
        else:
            for key in ("codigo_de_barras", "link"):
                if key not in parsed:
                    fail(
                        f"emitir_guia_regularizacao happy path: missing key '{key}'",
                        parsed,
                    )
    else:
        info(
            "Skipping emitir_guia_regularizacao happy path because PREVIEW_REGULARIZACAO_PAYLOAD is not set"
        )


def main() -> None:
    print(f"Running preview E2E checks against: {BASE_URL}")
    run_health_check()
    require_authenticated_env()
    run_consulta_happy_path()
    run_consulta_invalid_input_check()
    run_emitir_guia_minimal_payload_checks()
    run_emitir_guia_happy_paths()
    print("Preview E2E checks passed.")


if __name__ == "__main__":
    main()
