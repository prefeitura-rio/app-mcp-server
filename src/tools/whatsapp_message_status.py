"""
Tool de consulta de status de mensagens WhatsApp.

Permite verificar se uma mensagem foi lida (duplo check azul), entregue ou
enviada. Status é populado via webhook `/meta/webhook/status` (app.py) e
armazenado no Redis com TTL de 7 dias.

Ciclo de vida de status:
  sent → delivered → read
  (ou failed em qualquer ponto)

Docs Meta:
https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/components#statuses-object
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from src.utils.redis_client import get_redis_client


def check_message_read_status(message_id: str) -> dict[str, Any]:
    """
    Verifica se uma mensagem do WhatsApp foi lida (duplo check azul).

    O status é recebido via webhook e armazenado no Redis. Mensagens com
    mais de 7 dias não terão status disponível (TTL expirado).

    Args:
        message_id: ID da mensagem retornado ao enviar (formato wamid.xxx).
                    Exemplo: "wamid.HBgNNTUyMTE5NzUxNjU3MxUCABEYEjg3..."

    Returns:
        Dicionário com status da mensagem:
        {
            "message_id": "wamid.xxx",
            "found": True,
            "status": "read",           # sent|delivered|read|failed
            "is_read": True,             # Duplo check azul!
            "is_delivered": True,
            "is_sent": True,
            "timestamp": "1234567890",
            "recipient_id": "5521999999999",
            "updated_at": "1714567890"
        }

        Se não encontrado (mensagem muito antiga ou ID inválido):
        {
            "message_id": "wamid.xxx",
            "found": False,
            "error": "Status não encontrado..."
        }

    Exemplos:
        >>> check_message_read_status("wamid.HBgNNTUyMTE5NzUxNjU3MxUCABEYEjg3...")
        {
            "message_id": "wamid.HBgNNTUyMTE5NzUxNjU3MxUCABEYEjg3...",
            "found": True,
            "status": "read",
            "is_read": True,
            "is_delivered": True,
            "is_sent": True
        }
    """
    if not message_id:
        return {
            "message_id": "",
            "found": False,
            "error": "message_id obrigatório",
        }

    redis = get_redis_client()
    key = f"msg_status:{message_id}"

    try:
        data = redis.hgetall(key)
    except Exception as e:
        logger.error(
            "redis_error_on_status_check",
            message_id=message_id,
            error=str(e),
        )
        return {
            "message_id": message_id,
            "found": False,
            "error": f"Erro ao consultar Redis: {e}",
        }

    if not data:
        logger.info(
            "message_status_not_found",
            message_id=message_id,
        )
        return {
            "message_id": message_id,
            "found": False,
            "error": (
                "Status não encontrado. Possíveis causas: "
                "(1) mensagem enviada há mais de 7 dias (TTL expirado), "
                "(2) ID inválido, "
                "(3) webhook de status não configurado ou não recebido."
            ),
        }

    status = data.get("status", "")

    result = {
        "message_id": message_id,
        "found": True,
        "status": status,
        "is_read": status == "read",
        "is_delivered": status in ["delivered", "read"],
        "is_sent": status in ["sent", "delivered", "read"],
        "timestamp": data.get("timestamp", ""),
        "recipient_id": data.get("recipient_id", ""),
        "updated_at": data.get("updated_at", ""),
    }

    logger.info(
        "message_status_retrieved",
        message_id=message_id,
        status=status,
    )

    return result
