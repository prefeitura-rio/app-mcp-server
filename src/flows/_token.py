"""
Encode/decode de dados de pre-fill no `flow_token` do WhatsApp Flow.

O agente (LLM) é responsável por extrair entidades da mensagem do cidadão
antes de enviar o Flow. Este módulo serializa essas entidades no `flow_token`,
único canal que o Meta entrega ao endpoint server no INIT request quando o
Flow é dinâmico (`data_api_version: 3.0`).

Formato canônico:
    flow_token = "v1:" + base64url(json.dumps(prefill_dict))

Decisões de design:
- Versionamento explícito (`v1:`) permite evoluir formato futuramente
  (`v2:` etc.) sem quebra retroativa.
- base64url(JSON) suporta qualquer tipo (string/bool/number/null) sem
  conflito com separadores.
- Pra logs: NUNCA serialize token v1:* cru — payload pode conter PII
  (endereço, CPF). Use `redact_flow_token`.
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

    Payload encoded:
        {"_session": "<base_token>", **prefill_dict}

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
    payload_dict: dict[str, Any] = {**prefill, "_session": base_token}
    payload = json.dumps(payload_dict, sort_keys=True, ensure_ascii=False).encode(
        "utf-8"
    )
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{TOKEN_PREFIX}{encoded}"


def decode_flow_token(flow_token: str | None) -> dict[str, Any]:
    """
    Decoda prefill do flow_token v1:base64. Tokens opacos retornam {}.

    Returns:
        dict com entidades extraídas. Dict vazio se: token None, sem prefix
        v1:, ou decode/parse falha (always best-effort; never raise).

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
