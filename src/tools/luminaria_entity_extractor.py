"""
Encode/decode de dados de pre-fill no `flow_token` do WhatsApp Flow.

O agente (LLM) é responsável por extrair entidades da mensagem do cidadão
antes de enviar o Flow. Este módulo serializa essas entidades no `flow_token`,
único canal que o Meta entrega ao endpoint server no INIT request quando o
Flow é dinâmico (`data_api_version: 3.0`) — e que também sobrevive em Flow
estático via consumo no MCP `_handle_init`.

Formato canônico:
    flow_token = "v1:" + base64url(json.dumps(prefill_dict))

Decisões de design:
- Versionamento explícito (`v1:`) permite evoluir formato futuramente
  (`v2:` etc.) sem quebra retroativa.
- base64url(JSON) suporta qualquer tipo (string/bool/number/null) sem
  conflito com separadores. Alternativas pipe-delimited (`key=val|key=val`)
  quebram em valores contendo `=` ou `|` e perdem typing.
- Pra logs: NUNCA serialize token v1:* cru — payload pode conter PII
  (endereço, CPF). Use `_redact_flow_token` em `whatsapp_flow_sender`.
- Tokens sem prefix `v1:` são tratados como opacos (compat com bot legacy
  que envia apenas UUID); funções de decode retornam dict vazio.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any


TOKEN_PREFIX = "v1:"


def encode_flow_token(base_token: str, prefill: dict[str, Any] | None) -> str:
    """
    Encoda prefill data no flow_token preservando o `base_token` (UUID)
    como identificador único de sessão dentro do payload encoded.

    Hoje **NÃO é usado pelo path principal** — `send_flow_by_service` passa
    prefill via `flow_action_payload.data` direto (caminho funcional pra
    Flow estático). Esta função fica como utility pra:
    - Defensive fallback se Flow voltar a ser dinâmico (`data_api_version: 3.0`)
    - Compat com outros consumers que precisem encoded token

    Payload encoded:
        {"_session": "<base_token>", **prefill_dict}

    Inclusão do `_session` garante UUID único mesmo quando dois cidadãos
    têm prefills idênticos (codex P2 review 2026-05-19).

    Args:
        base_token: UUID-like correlação da session (não-sensível)
        prefill: dict de campos extraídos do contexto (defect_type, etc.)
                 None ou vazio retorna `base_token` sem encoding (opaco).

    Returns:
        Token v1:<base64> se prefill populado; `base_token` cru caso contrário.

    Examples:
        >>> encode_flow_token("uuid-x", {"defect_type": "Pendurada"})
        'v1:eyJfc2Vzc2lvbiI6ICJ1dWlkLXgiLCAiZGVmZWN0X3R5cGUiOiAiUGVuZHVyYWRhIn0'
        >>> encode_flow_token("uuid-x", None)
        'uuid-x'
        >>> encode_flow_token("uuid-x", {})
        'uuid-x'
    """
    if not prefill:
        return base_token
    # `_session` é reservado pra correlação interna: escreve DEPOIS do
    # prefill pra que LLM/caller não possa sobrescrever acidentalmente
    # (codex P3 round 6). Mesmo prefill em sessions diferentes ainda gera
    # tokens distintos via `_session`.
    payload_dict: dict[str, Any] = {**prefill, "_session": base_token}
    # JSON com keys sorted pra determinismo (mesmo dict + mesmo session → mesmo token).
    payload = json.dumps(payload_dict, sort_keys=True, ensure_ascii=False).encode(
        "utf-8"
    )
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{TOKEN_PREFIX}{encoded}"


def decode_flow_token(flow_token: str | None) -> dict[str, Any]:
    """
    Decoda prefill do flow_token v1:base64. Tokens opacos retornam {}.

    Args:
        flow_token: string como `"v1:eyJ..."` ou opaco como `"uuid-x"`.

    Returns:
        dict com entidades extraídas. Dict vazio se: token None, sem prefix
        v1:, ou decode/parse falha (sempre best-effort; never raise).

    Examples:
        >>> decode_flow_token("v1:eyJkZWZlY3RfdHlwZSI6ICJQZW5kdXJhZGEifQ")
        {'defect_type': 'Pendurada'}
        >>> decode_flow_token("uuid-opaco")
        {}
        >>> decode_flow_token(None)
        {}
    """
    if not isinstance(flow_token, str) or not flow_token.startswith(TOKEN_PREFIX):
        return {}
    encoded = flow_token[len(TOKEN_PREFIX) :]
    # base64url tolerante a padding ausente (rstrip("=") no encode)
    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        decoded_bytes = base64.urlsafe_b64decode(padded)
        decoded = json.loads(decoded_bytes.decode("utf-8"))
    except (binascii.Error, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    return decoded


def redact_flow_token(flow_token: str | None) -> str:
    """
    Mascara token pra logs. Tokens v1:* carregam JSON encoded com possível
    PII (endereço, CPF, etc.). Retorna marker + length, nunca payload cru.

    Tokens opacos (UUID) não são sensíveis — mostra prefixo curto pra
    correlação cross-log.
    """
    if not isinstance(flow_token, str):
        return "<none>"
    if flow_token.startswith(TOKEN_PREFIX):
        return f"{TOKEN_PREFIX}<redacted len={len(flow_token) - len(TOKEN_PREFIX)}>"
    return f"{flow_token[:8]}…" if len(flow_token) > 12 else flow_token
