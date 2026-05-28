"""
Configuração programática de webhooks WhatsApp via Graph API.

Permite subscrever a app aos eventos de webhook do WhatsApp Business Account
sem precisar acessar o Meta Business Manager. Usa a Graph API diretamente.

Schema Meta:
  POST /{WABA_ID}/subscribed_apps - subscreve campos
  GET  /{WABA_ID}/subscribed_apps  - lista subscrições
  GET  /{PHONE_ID}?fields=...      - pega WABA ID

Docs:
https://developers.facebook.com/docs/graph-api/webhooks/subscriptions
"""

from __future__ import annotations

from typing import Optional

import httpx
from loguru import logger

from src.config import env

_GRAPH_API_VERSION = "v23.0"
_TIMEOUT_S = 10.0


async def get_waba_id_from_phone(
    phone_number_id: Optional[str] = None,
    token: Optional[str] = None,
) -> str:
    """
    Descobre WhatsApp Business Account ID a partir do Phone Number ID.

    Args:
        phone_number_id: Phone Number ID (usa WA_PHONE_NUMBER_ID se não informado)
        token: Access token (usa WA_TOKEN se não informado)

    Returns:
        WABA ID (string numérica)

    Raises:
        Exception: quando API retorna erro ou campo ausente
    """
    phone_number_id = phone_number_id or env.WA_PHONE_NUMBER_ID
    token = token or env.WA_TOKEN

    if not phone_number_id:
        raise ValueError("WA_PHONE_NUMBER_ID não configurado")
    if not token:
        raise ValueError("WA_TOKEN não configurado")

    url = f"https://graph.facebook.com/{_GRAPH_API_VERSION}/{phone_number_id}"

    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        resp = await client.get(
            url,
            params={"fields": "whatsapp_business_account_id"},
            headers={"Authorization": f"Bearer {token}"},
        )

    if resp.status_code != 200:
        raise Exception(
            f"Erro ao buscar WABA ID: {resp.status_code} - {resp.text[:200]}"
        )

    data = resp.json()
    waba_id = data.get("whatsapp_business_account_id")
    if not waba_id:
        raise Exception(
            f"WABA ID não encontrado no response. Keys: {list(data.keys())}"
        )

    logger.info("waba_id_resolved", phone_number_id=phone_number_id, waba_id=waba_id)
    return waba_id


async def get_webhook_subscriptions(
    waba_id_or_phone_id: str,
    token: Optional[str] = None,
) -> dict:
    """
    Lista campos de webhook subscritos pra um WABA ou Phone Number.

    Args:
        waba_id_or_phone_id: WhatsApp Business Account ID ou Phone Number ID
        token: Access token (usa WA_TOKEN se não informado)

    Returns:
        Response da API (ex: {"data": [{"subscribed_fields": [...]}]})
    """
    token = token or env.WA_TOKEN
    if not token:
        raise ValueError("WA_TOKEN não configurado")

    url = f"https://graph.facebook.com/{_GRAPH_API_VERSION}/{waba_id_or_phone_id}/subscribed_apps"

    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
        )

    if resp.status_code != 200:
        logger.error(
            "get_subscriptions_failed",
            id=waba_id_or_phone_id,
            status=resp.status_code,
            error=resp.text[:200],
        )
        return {"error": resp.text, "status_code": resp.status_code}

    data = resp.json()
    logger.info("webhook_subscriptions_retrieved", id=waba_id_or_phone_id, data=data)
    return data


async def subscribe_webhook_fields(
    waba_id: Optional[str] = None,
    phone_number_id: Optional[str] = None,
    fields: Optional[list[str]] = None,
    token: Optional[str] = None,
) -> dict:
    """
    Subscreve a app aos campos de webhook do WhatsApp Business Account.

    Args:
        waba_id: WhatsApp Business Account ID (opcional se phone_number_id fornecido)
        phone_number_id: Phone Number ID (usa WA_PHONE_NUMBER_ID se não informado)
        fields: Campos pra subscrever. Default: ["messages", "message_statuses"]
                (messages = inbound, message_statuses = sent/delivered/read)
        token: Access token (usa WA_TOKEN se não informado)

    Returns:
        {"success": True/False, "subscribed_fields": [...], "error": "..."}
    """
    token = token or env.WA_TOKEN
    phone_number_id = phone_number_id or env.WA_PHONE_NUMBER_ID
    fields = fields or ["messages", "message_statuses"]

    if not token:
        raise ValueError("WA_TOKEN não configurado")

    # Usar phone_number_id diretamente (mais simples que buscar WABA)
    if not waba_id and not phone_number_id:
        raise ValueError("waba_id ou phone_number_id obrigatório")

    target_id = waba_id or phone_number_id
    url = f"https://graph.facebook.com/{_GRAPH_API_VERSION}/{target_id}/subscribed_apps"

    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={"subscribed_fields": fields},
        )

    if resp.status_code != 200:
        logger.error(
            "subscribe_webhook_failed",
            waba_id=waba_id,
            fields=fields,
            status=resp.status_code,
            error=resp.text[:200],
        )
        return {
            "success": False,
            "error": resp.text,
            "status_code": resp.status_code,
        }

    data = resp.json()
    logger.info(
        "webhook_subscribed",
        waba_id=waba_id,
        fields=fields,
        response=data,
    )
    return {
        "success": True,
        "subscribed_fields": fields,
        "response": data,
    }
