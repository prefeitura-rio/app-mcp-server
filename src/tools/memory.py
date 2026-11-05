"""Tools to interact with a user's chat memories"""

from enum import Enum
from typing import Any, Dict, List, Optional, Union

import httpx
from pydantic import BaseModel, ValidationError

from src.config.env import RMI_API_URL
from src.utils.rmi_oauth2 import get_authorization_header, is_oauth2_configured
from src.utils.error_interceptor import interceptor
from src.utils.http_client import InterceptedHTTPClient
from src.utils.pii import resumir_texto_livre


class MemoryType(Enum):
    base = "base"
    appended = "appended"


class MemoryRelevance(Enum):
    low = "low"
    medium = "medium"
    high = "high"


class MemoryBank(BaseModel):
    memory_name: str
    description: str
    relevance: MemoryRelevance
    memory_type: MemoryType
    value: str


# Campos do corpo do PUT/POST à API de memória que o `InterceptedHTTPClient`
# reporta como texto livre: no `request_body` do error interceptor vão só o nome
# e o tamanho de cada valor, nunca o valor (DEV-315). Todos os campos do
# `MemoryBank`, para um campo novo não sair em claro por omissão.
_CAMPOS_DO_MEMORY_BANK = tuple(MemoryBank.model_fields)


def _input_body_do_get(args: tuple, kwargs: dict) -> Dict[str, Any]:
    """
    `input_body` do `get_memories` no error interceptor: só o tamanho e os tipos
    de dado pessoal do nome da memória, nunca o nome (DEV-336).

    O nome é escolhido pelo agente a partir da conversa (um rótulo de saúde ou
    de família) e é tratado como dado sensível. O `user_id` já vai, mascarado,
    no campo próprio do report.
    """
    if "memory_name" in kwargs:
        memory_name = kwargs["memory_name"]
    else:
        memory_name = args[1] if len(args) > 1 else None
    return {"memory_name": resumir_texto_livre(memory_name)}


@interceptor(
    source={"source": "mcp", "tool": "memory"},
    extract_user_id=lambda args, kwargs: (
        kwargs.get("user_id") or (args[0] if args else "unknown")
    ),
    extract_input_body=_input_body_do_get,
)
async def get_memories(
    user_id: str, memory_name: Optional[str] = None
) -> Union[dict, List[dict]]:
    """Get a user's memory bank.

    Args:
        user_id (str): The user's phone number.
        memory_name (str, optional): The name of the memory bank. If None is given, return a list of all memory banks. Defaults to None.

    Returns:
        Union[dict, List[dict]]: Memory bank list or single memory bank.
    """

    # GET /v1/memory/{user_id}
    # GET /v1/memory/{user_id}/{memory_name}

    # Handle empty strings as memory name
    if isinstance(memory_name, str) and len(memory_name.strip()) == 0:
        memory_name = None

    url = "{}/v1/memory/{}".format(RMI_API_URL, user_id)
    # O `api_endpoint` reportado ao interceptor é o rótulo, nunca o caminho com
    # o `user_id` e o nome da memória (DEV-336).
    report_endpoint = "{}/v1/memory/{{user_id}}".format(RMI_API_URL)
    if memory_name is not None:
        url += "/{}".format(memory_name)
        report_endpoint += "/{memory_name}"

    # Use OAuth2 if configured, otherwise return unauthorized error
    if not is_oauth2_configured():
        return {"status": "Error", "detail": "Unauthorized: OAuth2 not configured"}

    headers = {"Authorization": await get_authorization_header()}

    async with InterceptedHTTPClient(
        user_id=user_id,
        source={"source": "mcp", "tool": "memory"},
        timeout=120.0,
        report_endpoint=report_endpoint,
    ) as client:
        response = await client.get(url, headers=headers)

        # 404 = usuário ainda não tem memória salva — estado válido de negócio,
        # não uma falha. raise_for_status() aqui viraria isError=True no cliente MCP.
        if response.status_code == 404:
            return (
                []
                if memory_name is None
                else {"status": "not_found", "memory_name": memory_name}
            )

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            # A mensagem do httpx traz a URL com o `user_id` e o nome da memória,
            # e o `@interceptor` a reporta como `error_message` (DEV-336). Sem a
            # exceção original no traceback (`from None`).
            raise httpx.HTTPStatusError(
                "HTTP {} da API de memória em GET {}".format(
                    response.status_code, report_endpoint
                ),
                request=e.request,
                response=e.response,
            ) from None
        return response.json()


_MAX_CHARS_NOME_DA_CHAVE = 64


def _tamanho_do_valor(valor: Any) -> Dict[str, Any]:
    if isinstance(valor, str):
        return {"chars": len(valor)}
    return {"tipo": type(valor).__name__}


def _input_body_do_upsert(args: tuple, kwargs: dict) -> Dict[str, Any]:
    """
    `input_body` do `upsert_memory` no error interceptor: os nomes das chaves do
    `memory_bank` e o tamanho de cada valor, nunca o valor (DEV-280).

    O banco de memórias guarda o que o cidadão contou (saúde, família, endereço),
    e texto livre não se redige. O `user_id` já vai, mascarado, no campo próprio
    do report.
    Se a extração falhar, o `@interceptor` manda só o nome e o tipo dos argumentos.
    """
    memory_bank = kwargs["memory_bank"] if "memory_bank" in kwargs else args[1]
    if not isinstance(memory_bank, dict):
        return {"memory_bank": {"tipo": type(memory_bank).__name__}}
    return {
        "memory_bank": {
            str(chave)[:_MAX_CHARS_NOME_DA_CHAVE]: _tamanho_do_valor(valor)
            for chave, valor in memory_bank.items()
        }
    }


@interceptor(
    source={"source": "mcp", "tool": "memory"},
    extract_user_id=lambda args, kwargs: (
        kwargs.get("user_id") or (args[0] if args else "unknown")
    ),
    extract_input_body=_input_body_do_upsert,
)
async def upsert_memory(
    user_id: str,
    memory_bank: dict,
) -> dict:
    """Create or update a user's memory bank.

    Args:
        user_id (str): The user's phone number.
        memory_bank (dict): Memory bank data as a dictionary.

    Returns:
        dict: A dictionary with status of the operation.
    """

    # POST /v1/memory/{user_id}
    # PUT /v1/memory/{user_id}

    try:
        validated_memory_bank = MemoryBank(**memory_bank).model_dump(mode="json")
    except ValidationError:
        return {"status": "Error", "detail": "Invalid memory bank"}

    url = "{}/v1/memory/{}".format(RMI_API_URL, user_id)

    # Use OAuth2 if configured, otherwise return unauthorized error
    if not is_oauth2_configured():
        return {"status": "Error", "detail": "Unauthorized: OAuth2 not configured"}

    headers = {"Authorization": await get_authorization_header()}

    # Tries to update the memory bank
    async with InterceptedHTTPClient(
        user_id=user_id,
        source={"source": "mcp", "tool": "memory"},
        timeout=120.0,
        free_text_fields=_CAMPOS_DO_MEMORY_BANK,
    ) as client:
        try:
            response = await client.put(
                url, headers=headers, json=validated_memory_bank
            )
            response.raise_for_status()
            return response.json()
        # If the memory bank does not exist, creates it
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                response = await client.post(
                    url, headers=headers, json=validated_memory_bank
                )
                # 409 no POST = memória já existe com esse nome (race condition);
                # tratar como upsert bem-sucedido para não explodir o turno.
                if response.status_code == 409:
                    return {"status": "skipped", "reason": "conflict"}
                response.raise_for_status()
                return response.json()
            elif e.response.status_code == 409:
                # PUT retornou 409 — memória existe mas update conflitou;
                # não é falha crítica.
                return {"status": "skipped", "reason": "conflict"}
            else:
                raise
