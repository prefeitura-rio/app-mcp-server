#!/usr/bin/env python3
import json
import os
import socket
import sys
import time
import unicodedata
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
STALE_REGULARIZACAO_HINTS = (
    "nao ha parcelas em atraso",
    "não há parcelas em atraso",
)
# A PGM responde de forma intermitente "sem registros" ou fica indisponível para
# o mesmo payload que passa minutos depois (DEV-415). Nesses casos o happy path
# de `/emitir_guia` tenta de novo antes de concluir: uma regressão do nosso
# código falha em todas as tentativas, uma oscilação da PGM não.
EXTERNAL_API_MAX_ATTEMPTS = max(
    1, int(os.environ.get("PREVIEW_E2E_EXTERNAL_RETRIES", "3"))
)
EXTERNAL_API_RETRY_DELAY_SECONDS = max(
    0, int(os.environ.get("PREVIEW_E2E_EXTERNAL_RETRY_DELAY", "20"))
)
# Classes de `api_descricao_erro`, comparadas sem acento e em minúsculas.
# "sem_registros" vem da própria PGM; "indisponivel" só é produzida pelo
# timeout de `pgm_api`. As duas dizem que a PGM não respondeu ao payload agora,
# e esgotadas as tentativas viram SKIP explícito, como a fixture velha do
# `regularizacao`.
# "erro_generico" é a resposta do `except Exception` das rotas: pode ser
# oscilação da PGM, mas também uma exceção do nosso código, então tenta de novo
# e, se persistir, falha.
UPSTREAM_FAILURE_HINTS = (
    ("sem_registros", ("nao foram encontrados registros",)),
    ("indisponivel", ("temporariamente indisponivel",)),
    ("erro_generico", ("tente novamente mais tarde",)),
)
SKIPPABLE_UPSTREAM_FAILURES = frozenset({"sem_registros", "indisponivel"})


def is_stale_regularizacao_fixture(parsed) -> bool:
    if not isinstance(parsed, dict):
        return False
    if parsed.get("api_resposta_sucesso") is True:
        return False

    description = (parsed.get("api_descricao_erro") or "").lower()
    return any(hint in description for hint in STALE_REGULARIZACAO_HINTS)


def _normalize_text(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return decomposed.encode("ascii", "ignore").decode("ascii").lower()


def classify_upstream_failure(parsed) -> str | None:
    """
    Classe da falha da PGM numa resposta `api_resposta_sucesso=false`, ou
    `None` quando a resposta não é uma falha conhecida do upstream.
    """
    if not isinstance(parsed, dict):
        return None
    if parsed.get("api_resposta_sucesso") is not False:
        return None

    description = parsed.get("api_descricao_erro")
    if not isinstance(description, str):
        return None

    normalized = _normalize_text(description)
    for failure_class, hints in UPSTREAM_FAILURE_HINTS:
        if any(hint in normalized for hint in hints):
            return failure_class
    return None


def response_summary(status=None, raw=None, parsed=None, upstream=None) -> dict:
    """
    O que pode ir para o log público do Actions sobre uma resposta: status,
    `api_resposta_sucesso`, as chaves presentes e o tamanho do corpo.

    Nunca os valores: `api_descricao_erro` da PGM cita o número da execução
    fiscal, e a resposta de sucesso devolve o payload de entrada com os
    identificadores do contribuinte (AGENTS.md, dados pessoais).
    """
    summary: dict = {}
    if status is not None:
        summary["http_status"] = status
    if isinstance(parsed, dict):
        sucesso = parsed.get("api_resposta_sucesso")
        summary["api_resposta_sucesso"] = (
            sucesso if isinstance(sucesso, bool) or sucesso is None else "non-bool"
        )
        summary["keys"] = sorted(str(key) for key in parsed)
    elif isinstance(parsed, list):
        summary["json_type"] = "list"
        summary["length"] = len(parsed)
    elif raw is not None:
        summary["json_type"] = "none"
        summary["body_length"] = len(raw)
    if upstream is not None:
        summary["upstream_failure"] = upstream
    return summary


def fail(message: str, details=None) -> None:
    """
    `details` vai inteiro para o log: passe só `response_summary(...)` ou
    dados sem conteúdo de resposta nem de segredo.
    """
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
        fail(f"{name}: expected JSON object", {"json_type": type(value).__name__})

    return value


def require_status(
    actual: int, expected: int, context: str, raw=None, parsed=None
) -> None:
    if actual != expected:
        fail(
            f"{context}: expected HTTP {expected}, got {actual}",
            response_summary(actual, raw, parsed),
        )


def require_json_object(payload, context: str, raw=None):
    if not isinstance(payload, dict):
        fail(
            f"{context}: expected JSON object response",
            response_summary(raw=raw, parsed=payload),
        )


def run_health_check() -> None:
    info("Checking /health")
    status, raw = request_text("/health")
    require_status(status, 200, "health", raw)
    if raw.strip().upper() != "OK":
        fail("health: unexpected body", response_summary(status, raw))


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


def run_consulta_happy_path() -> None:
    info("Running authenticated consulta_debitos happy path")
    auth_token = get_auth_token()
    status, raw, parsed = request_json(
        "/consulta_debitos",
        payload=build_consulta_payload(valid=True),
        token=auth_token,
    )
    require_status(status, 200, "consulta_debitos happy path", raw, parsed)
    require_json_object(parsed, "consulta_debitos happy path", raw)

    if parsed.get("api_resposta_sucesso") is not True:
        fail(
            "consulta_debitos happy path: expected api_resposta_sucesso=true",
            response_summary(status, raw, parsed),
        )

    required_keys = (
        "dicionario_itens",
        "total_itens_pagamento",
        "debitos_msg",
        "mensagem_divida_contribuinte",
    )
    for key in required_keys:
        if key not in parsed:
            fail(
                f"consulta_debitos happy path: missing key '{key}'",
                response_summary(status, raw, parsed),
            )


def run_consulta_invalid_input_check() -> None:
    info("Running authenticated consulta_debitos invalid-input path")
    auth_token = get_auth_token()
    status, raw, parsed = request_json(
        "/consulta_debitos",
        payload=build_consulta_payload(valid=False),
        token=auth_token,
    )
    require_status(status, 200, "consulta_debitos invalid input", raw, parsed)
    require_json_object(parsed, "consulta_debitos invalid input", raw)

    if parsed.get("api_resposta_sucesso") is not False:
        fail(
            "consulta_debitos invalid input: expected api_resposta_sucesso=false",
            response_summary(status, raw, parsed),
        )
    if "api_descricao_erro" not in parsed:
        fail(
            "consulta_debitos invalid input: missing api_descricao_erro",
            response_summary(status, raw, parsed),
        )


def run_emitir_guia_minimal_payload_checks() -> None:
    info("Checking guia endpoints with minimal payload")
    auth_token = get_auth_token()
    for route in ("/emitir_guia", "/emitir_guia_regularizacao"):
        status, raw, parsed = request_json(route, payload={}, token=auth_token)
        require_status(status, 200, f"{route} invalid payload", raw, parsed)
        require_json_object(parsed, f"{route} minimal payload", raw)
        if "api_resposta_sucesso" not in parsed:
            fail(
                f"{route} minimal payload: missing api_resposta_sucesso",
                response_summary(status, raw, parsed),
            )


def request_guide_with_upstream_retry(path: str, payload, token: str, label: str):
    """
    Chama a rota de emissão e repete só quando a PGM respondeu com uma falha
    conhecida do upstream (`classify_upstream_failure`). Sucesso nunca se
    repete: cada emissão bem-sucedida gera uma guia real. Devolve
    `(status, raw, parsed, upstream, attempts)` da última chamada.
    """
    attempts = EXTERNAL_API_MAX_ATTEMPTS
    for attempt in range(1, attempts + 1):
        status, raw, parsed = request_json(
            path,
            payload=payload,
            token=token,
            timeout=GUIDE_POST_TIMEOUT,
        )
        upstream = classify_upstream_failure(parsed) if status == 200 else None
        if upstream is None or attempt == attempts:
            return status, raw, parsed, upstream, attempt

        info(
            f"{label}: attempt {attempt}/{attempts} got upstream failure "
            f"'{upstream}' from PGM; retrying in {EXTERNAL_API_RETRY_DELAY_SECONDS}s"
        )
        time.sleep(EXTERNAL_API_RETRY_DELAY_SECONDS)

    raise AssertionError("unreachable: EXTERNAL_API_MAX_ATTEMPTS >= 1")


def run_emitir_guia_happy_paths() -> None:
    auth_token = get_auth_token()
    avista_payload = load_json_env("PREVIEW_AVISTA_PAYLOAD", AVISTA_PAYLOAD)
    if avista_payload:
        info("Running authenticated emitir_guia happy path")
        status, raw, parsed, upstream, attempts = request_guide_with_upstream_retry(
            "/emitir_guia",
            avista_payload,
            auth_token,
            "emitir_guia happy path",
        )
        require_status(status, 200, "emitir_guia happy path", raw, parsed)
        require_json_object(parsed, "emitir_guia happy path", raw)
        if parsed.get("api_resposta_sucesso") is not True:
            if upstream in SKIPPABLE_UPSTREAM_FAILURES:
                info(
                    "emitir_guia happy path: SKIPPED - PGM answered "
                    f"'{upstream}' in {attempts} attempt(s); the response kept the "
                    "error contract (HTTP 200, api_resposta_sucesso=false, "
                    "api_descricao_erro). If it repeats, check "
                    "PREVIEW_AVISTA_PAYLOAD em Infisical."
                )
            else:
                fail(
                    "emitir_guia happy path: expected api_resposta_sucesso=true",
                    response_summary(status, raw, parsed, upstream),
                )
        else:
            for key in ("codigo_de_barras", "link"):
                if key not in parsed:
                    fail(
                        f"emitir_guia happy path: missing key '{key}'",
                        response_summary(status, raw, parsed),
                    )
    else:
        info(
            "Skipping emitir_guia happy path because PREVIEW_AVISTA_PAYLOAD is not set"
        )

    regularizacao_payload = load_json_env(
        "PREVIEW_REGULARIZACAO_PAYLOAD", REGULARIZACAO_PAYLOAD
    )
    if regularizacao_payload:
        info("Running authenticated emitir_guia_regularizacao happy path")
        status, raw, parsed = request_json(
            "/emitir_guia_regularizacao",
            payload=regularizacao_payload,
            token=auth_token,
            timeout=GUIDE_POST_TIMEOUT,
        )
        require_status(status, 200, "emitir_guia_regularizacao happy path", raw, parsed)
        require_json_object(parsed, "emitir_guia_regularizacao happy path", raw)
        if parsed.get("api_resposta_sucesso") is not True:
            if is_stale_regularizacao_fixture(parsed):
                info(
                    "emitir_guia_regularizacao happy path: SKIPPED - fixture stale "
                    "(upstream: no overdue installments). "
                    "Update PREVIEW_REGULARIZACAO_PAYLOAD em Infisical."
                )
                return

            fail(
                "emitir_guia_regularizacao happy path: expected api_resposta_sucesso=true",
                response_summary(status, raw, parsed),
            )
        for key in ("codigo_de_barras", "link"):
            if key not in parsed:
                fail(
                    f"emitir_guia_regularizacao happy path: missing key '{key}'",
                    response_summary(status, raw, parsed),
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
