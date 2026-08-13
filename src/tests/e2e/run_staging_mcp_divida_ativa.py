#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import sys
from uuid import uuid4

from langchain_mcp_adapters.client import MultiServerMCPClient


SERVICE_NAME = "divida_ativa"
TOOL_NAME = "multi_step_service"
DEFAULT_PAYLOADS = [{}]


def fail(message: str, details=None) -> None:
    print(f"FAIL: {message}")
    if details is not None:
        if isinstance(details, (dict, list)):
            print(json.dumps(details, ensure_ascii=False, indent=2))
        else:
            print(details)
    sys.exit(1)


def info(message: str) -> None:
    print(f"- {message}")


def load_payloads(raw_payloads: str | None) -> list[dict]:
    if not raw_payloads:
        return DEFAULT_PAYLOADS

    try:
        payloads = json.loads(raw_payloads)
    except json.JSONDecodeError as exc:
        fail("DIVIDA_ATIVA_STAG_PAYLOADS deve ser JSON valido", str(exc))

    if isinstance(payloads, dict):
        return [payloads]

    if not isinstance(payloads, list) or not all(
        isinstance(payload, dict) for payload in payloads
    ):
        fail("DIVIDA_ATIVA_STAG_PAYLOADS deve ser um objeto JSON ou lista de objetos")

    return payloads


def clean_env_value(value: str) -> str:
    value = value.strip()
    if value.startswith(("'", '"')):
        quote = value[0]
        end = value.find(quote, 1)
        if end != -1:
            return value[1:end]
    return value.strip().strip("\\").strip().strip("\"'")


def read_env_file_value(name: str) -> str:
    env_path = os.environ.get("ENV_FILE", ".env")
    if not os.path.exists(env_path):
        return ""

    with open(env_path) as env_file:
        for line in env_file:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue

            key, value = stripped.split("=", 1)
            if key.strip() == name:
                return clean_env_value(value)

    return ""


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip() or read_env_file_value(name)
    if not value:
        fail(f"Variavel de ambiente obrigatoria ausente: {name}")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Chama a tool multi_step_service em staging usando service_name=divida_ativa."
        )
    )
    parser.add_argument(
        "--payloads",
        default=os.environ.get("DIVIDA_ATIVA_STAG_PAYLOADS"),
        help=(
            "JSON object ou lista de objetos para enviar sequencialmente. "
            "Padrao: [{}]. Tambem pode ser definido via DIVIDA_ATIVA_STAG_PAYLOADS."
        ),
    )
    parser.add_argument(
        "--user-id",
        default=os.environ.get("DIVIDA_ATIVA_STAG_USER_ID"),
        help=(
            "user_id usado no workflow. Padrao: valor unico gerado para esta execucao."
        ),
    )
    return parser.parse_args()


async def get_multi_step_tool(mcp_url: str, api_token: str):
    client = MultiServerMCPClient(
        {
            "rio_mcp_stag": {
                "transport": "streamable_http",
                "url": mcp_url,
                "headers": {"Authorization": f"Bearer {api_token}"},
            }
        }
    )
    tools = await client.get_tools()
    for tool in tools:
        if tool.name == TOOL_NAME:
            return tool
    fail(
        f"Tool '{TOOL_NAME}' nao encontrada no MCP de staging",
        {"tools": [tool.name for tool in tools]},
    )


def require_valid_response(response: dict, step_index: int) -> None:
    if not isinstance(response, dict):
        fail(f"Step {step_index}: resposta nao e objeto JSON", response)

    if response.get("service_name") != SERVICE_NAME:
        fail(f"Step {step_index}: service_name inesperado", response)

    if response.get("error_message"):
        fail(f"Step {step_index}: workflow retornou erro", response)

    if "description" not in response:
        fail(f"Step {step_index}: resposta sem description", response)

    if "payload_schema" not in response:
        fail(f"Step {step_index}: resposta sem payload_schema", response)


def normalize_response(response) -> dict:
    if isinstance(response, dict):
        return response

    if isinstance(response, str):
        try:
            parsed = json.loads(response)
        except json.JSONDecodeError:
            return {"raw_response": response}
        if isinstance(parsed, dict):
            return parsed

    return {"raw_response": response}


def require_initial_schema(response: dict) -> None:
    schema = response.get("payload_schema") or {}
    properties = schema.get("properties") or {}
    tipo_consulta = properties.get("tipo_consulta") or {}

    if "tipo_consulta" not in properties:
        fail("Resposta inicial nao retornou schema de tipo_consulta", response)

    expected_options = {
        "cpf_cnpj",
        "inscricao_imobiliaria",
        "auto_infracao",
        "cda",
        "execucao_fiscal",
    }
    actual_options = set(tipo_consulta.get("enum") or [])
    if actual_options != expected_options:
        fail(
            "Schema inicial de divida ativa retornou opcoes inesperadas",
            {"expected": sorted(expected_options), "actual": sorted(actual_options)},
        )


async def run() -> None:
    args = parse_args()
    mcp_url = require_env("MCP_URL_STAG")
    api_token = require_env("MCP_API_TOKEN_STAG")
    user_id = args.user_id or f"e2e-divida-ativa-stag-{uuid4()}"
    payloads = load_payloads(args.payloads)

    info(f"Conectando ao MCP staging: {mcp_url}")
    tool = await get_multi_step_tool(mcp_url, api_token)

    last_response = None
    for index, payload in enumerate(payloads, start=1):
        info(f"Step {index}: chamando {TOOL_NAME}({SERVICE_NAME})")
        response = await tool.ainvoke(
            {
                "service_name": SERVICE_NAME,
                "user_id": user_id,
                "payload_json": json.dumps(payload, ensure_ascii=False),
            }
        )
        response = normalize_response(response)
        require_valid_response(response, index)
        last_response = response

    if payloads == DEFAULT_PAYLOADS and last_response is not None:
        require_initial_schema(last_response)

    print("PASS: multi_step_service(divida_ativa) respondeu corretamente em staging")
    if last_response is not None:
        print(json.dumps(last_response, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(run())
