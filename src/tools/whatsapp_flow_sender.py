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

    async def send_flow_template(
        self,
        recipient: str,
        template_name: str,
        flow_token: str | None = None,
        language_code: str = "pt_BR",
    ) -> Dict[str, Any]:
        """
        Envia template com flow button para um destinatário.

        Args:
            recipient: Número do destinatário no formato E.164 sem + (ex: 5521999999999)
            template_name: Nome do template aprovado na Meta (ex: 152_reparo_luminaria)
            flow_token: Identificador único da sessão (default: UUID gerado)
            language_code: Código do idioma do template (default: pt_BR)

        Returns:
            Resposta da API do WhatsApp com message_id

        Raises:
            httpx.HTTPError: Se falhar o envio
        """
        if not flow_token:
            flow_token = str(uuid.uuid4())

        # Remove + se vier no número
        recipient = recipient.replace("+", "")

        logger.info(
            f"Enviando WhatsApp Flow | recipient={recipient} | "
            f"template={template_name} | flow_token={flow_token}"
        )

        payload = {
            "messaging_product": "whatsapp",
            "to": recipient,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language_code},
                "components": [
                    {
                        "type": "button",
                        "sub_type": "flow",
                        "index": "0",
                        "parameters": [
                            {
                                "type": "action",
                                "action": {"flow_token": flow_token},
                            }
                        ],
                    }
                ],
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
                f"WhatsApp Flow enviado | message_id={message_id} | flow_token={flow_token}"
            )

            return {
                "success": True,
                "message_id": message_id,
                "flow_token": flow_token,
                "recipient": recipient,
                "template_name": template_name,
            }


async def send_luminaria_flow(
    user_number: str,
    flow_token: str | None = None,
) -> Dict[str, Any]:
    """
    Dispara WhatsApp Flow de reparo de luminária para um usuário.

    Args:
        user_number: Número do usuário no formato E.164 sem + (ex: 5521999999999)
        flow_token: Token opcional de rastreamento da sessão

    Returns:
        Resultado do envio com message_id e flow_token
    """
    sender = WhatsAppFlowSender()

    try:
        result = await sender.send_flow_template(
            recipient=user_number,
            template_name="152_reparo_luminaria",
            flow_token=flow_token,
        )

        return result

    except httpx.HTTPError as e:
        logger.error(f"Erro ao enviar flow de luminária: {e}")
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
