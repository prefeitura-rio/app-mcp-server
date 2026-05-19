"""
Tool para disparar WhatsApp Flow templates via Meta WhatsApp Business API.

Usado para enviar formulários interativos (flows) para coleta estruturada
de dados do cidadão antes de iniciar workflows conversacionais.
"""

import uuid
from typing import Any, Dict

import httpx
from loguru import logger

from src.config import env


def _redact_flow_token(flow_token: str | None) -> str:
    """
    Mascara flow_token pra logs. Tokens `v1:*` carregam prefill JSON
    encoded em base64url, podendo conter PII (endereço, CPF, etc.).
    Retorna marker + length, nunca o conteúdo do payload encoded.
    """
    if not isinstance(flow_token, str):
        return "<none>"
    if flow_token.startswith("v1:"):
        return f"v1:<redacted len={len(flow_token) - 3}>"
    # Tokens opacos (uuid) não são sensíveis — mostra prefixo curto pra correlação
    return f"{flow_token[:8]}…" if len(flow_token) > 12 else flow_token


class WhatsAppFlowSender:
    """Cliente para enviar templates do WhatsApp Business com Flow buttons."""

    def __init__(
        self,
        token: str | None = None,
        phone_number_id: str | None = None,
    ):
        self.token = token or getattr(env, "WA_TOKEN", None)
        self.phone_number_id = phone_number_id or getattr(
            env, "WA_PHONE_NUMBER_ID", None
        )

        if not self.token:
            raise ValueError("WA_TOKEN não configurado no .env")
        if not self.phone_number_id:
            raise ValueError("WA_PHONE_NUMBER_ID não configurado no .env")

        self.base_url = f"https://graph.facebook.com/v20.0/{self.phone_number_id}"

    async def send_flow(
        self,
        recipient: str,
        flow_id: str,
        flow_token: str | None = None,
        flow_cta: str = "Abrir",
    ) -> Dict[str, Any]:
        """
        Envia WhatsApp Flow interativo para um destinatário.

        Args:
            recipient: Número do destinatário no formato E.164 sem + (ex: 5521999999999)
            flow_id: ID do flow cadastrado na Meta (ex: 4141008006029185)
            flow_token: Identificador único da sessão (default: UUID gerado)
            flow_cta: Texto do botão de CTA (default: "Abrir")

        Returns:
            Resposta da API do WhatsApp com message_id

        Raises:
            httpx.HTTPError: Se falhar o envio
        """
        if not flow_token:
            flow_token = str(uuid.uuid4())

        # Remove + se vier no número
        recipient = recipient.replace("+", "")

        # Token v1:* pode carregar prefill JSON com PII (endereço, CPF, etc).
        # Log só o prefix pra correlação + comprimento, nunca o valor cru.
        logged_token = _redact_flow_token(flow_token)
        logger.info(
            f"Enviando WhatsApp Flow | recipient={recipient} | "
            f"flow_id={flow_id} | flow_token={logged_token}"
        )

        payload = {
            "messaging_product": "whatsapp",
            "to": recipient,
            "type": "interactive",
            "interactive": {
                "type": "flow",
                "body": {"text": "Por favor, me dê mais detalhes sobre a luminária."},
                "action": {
                    "name": "flow",
                    "parameters": {
                        "flow_message_version": "3",
                        "flow_id": flow_id,
                        "flow_token": flow_token,
                        "flow_cta": flow_cta,
                        "flow_action": "navigate",
                        "flow_action_payload": {"screen": "MAIN"},
                    },
                },
            },
        }

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.base_url}/messages",
                json=payload,
                headers=headers,
            )

            if response.status_code != 200:
                error_data = response.json() if response.text else {}
                logger.error(
                    f"Erro ao enviar WhatsApp Flow: {response.status_code} - {error_data}"
                )
                response.raise_for_status()

            result = response.json()
            message_id = result.get("messages", [{}])[0].get("id")

            logger.success(
                f"WhatsApp Flow enviado | message_id={message_id} | flow_token={logged_token}"
            )

            return {
                "success": True,
                "message_id": message_id,
                "flow_token": flow_token,
                "recipient": recipient,
                "flow_id": flow_id,
            }


# Mapeamento de service_type para flow_id cadastrado na Meta
# Para adicionar novo flow: registrar no Meta, pegar o flow_id e adicionar aqui
FLOW_TEMPLATES = {
    "reparo_luminaria": "4141008006029185",
    # Adicionar novos flows aqui conforme forem criados na Meta
    # "poda_arvore": "FLOW_ID_AQUI",
    # "limpeza_urbana": "FLOW_ID_AQUI",
}


async def send_flow_by_service(
    service_type: str,
    user_number: str,
    flow_token: str | None = None,
) -> Dict[str, Any]:
    """
    Dispara WhatsApp Flow apropriado para um tipo de serviço.

    Args:
        service_type: Tipo de serviço (ex: reparo_luminaria, poda_arvore)
        user_number: Número do usuário no formato E.164 sem + (ex: 5521999999999)
        flow_token: Token opcional de rastreamento da sessão

    Returns:
        Resultado do envio com message_id e flow_token
    """
    flow_id = FLOW_TEMPLATES.get(service_type)

    if not flow_id:
        available = ", ".join(FLOW_TEMPLATES.keys())
        return {
            "success": False,
            "error": f"Flow não cadastrado para service_type='{service_type}'",
            "message": f"Flow disponível apenas para: {available}",
        }

    sender = WhatsAppFlowSender()

    try:
        result = await sender.send_flow(
            recipient=user_number,
            flow_id=flow_id,
            flow_token=flow_token,
        )

        return result

    except httpx.HTTPError as e:
        logger.error(f"Erro ao enviar flow de {service_type}: {e}")
        return {
            "success": False,
            "error": str(e),
            "message": "Não foi possível enviar o formulário. Vamos continuar por texto.",
        }
    except Exception as e:
        logger.error(f"Erro inesperado ao enviar flow: {e}")
        return {
            "success": False,
            "error": str(e),
            "message": "Erro ao processar o envio do formulário.",
        }


# Compatibilidade com código existente
async def send_luminaria_flow(
    user_number: str,
    flow_token: str | None = None,
) -> Dict[str, Any]:
    """Wrapper para compatibilidade. Use send_flow_by_service."""
    return await send_flow_by_service("reparo_luminaria", user_number, flow_token)
