"""
Tool para disparar WhatsApp Flow templates via Meta WhatsApp Business API.

Usado para enviar formulários interativos (flows) para coleta estruturada
de dados do cidadão antes de iniciar workflows conversacionais.
"""

import uuid
from typing import Any, Dict, Optional

import httpx
from loguru import logger

from src.config import env
from src.flows._token import (
    encode_flow_token,
    redact_flow_token as _redact_flow_token,
)
from src.tools.whatsapp_flows.normalizers import normalize_prefill_for_flow


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
        prefill_data: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """
        Envia WhatsApp Flow interativo para um destinatário.

        Args:
            recipient: Número do destinatário no formato E.164 sem + (ex: 5521999999999)
            flow_id: ID do flow cadastrado na Meta (ex: 4141008006029185)
            flow_token: Identificador único da sessão (default: UUID gerado)
            flow_cta: Texto do botão de CTA (default: "Abrir")
            prefill_data: Valores pra pré-popular campos do Flow. Vão pra
                `flow_action_payload.data` — caminho funcional pra Flow
                ESTÁTICO (cliente WhatsApp consome direto via Form
                `init-values: ${data.X_prefill}` no Flow JSON). Empiricamente
                provado E2E em produção 2026-05-19.

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
            f"flow_id={flow_id} | flow_token={logged_token} | "
            f"prefill_keys={sorted(prefill_data.keys()) if prefill_data else []}"
        )

        # Pra Flow estático: cliente WhatsApp aplica `data` direto nos
        # Form `init-values`. Pra Flow dinâmico: Meta entrega esse data ao
        # endpoint server no INIT — geralmente vazio em v3.0, mas defensive.
        flow_action_payload: Dict[str, Any] = {"screen": "MAIN"}
        if prefill_data:
            # Normalização: convenção do Flow JSON é declarar
            # `screen.data` com sufixo `_prefill` (ex: `endereco_prefill`)
            # consumido em `init-values: ${data.endereco_prefill}`. Callers
            # podem passar nome canônico do field (`endereco`) ou já com
            # sufixo (`endereco_prefill`); aqui normalizamos pra sempre
            # bater com o schema do Flow.
            normalized: Dict[str, Any] = {}
            for key, value in prefill_data.items():
                canonical = key if key.endswith("_prefill") else f"{key}_prefill"
                normalized[canonical] = value
            flow_action_payload["data"] = normalized

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
                        "flow_action_payload": flow_action_payload,
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

    async def send_interactive(
        self, recipient: str, interactive: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Envia um objeto `interactive` genérico (button/list/cta) pro Meta.

        Reusa auth/host de send_flow. `interactive` é o bloco já montado (ex:
        saída de build_buttons_envelope()["interactive"]). NÃO levanta em erro
        HTTP: retorna {"success": False, ...} pra o caller cair em fallback de
        texto — a mensagem interativa é best-effort.
        """
        recipient = recipient.replace("+", "")
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient,
            "type": "interactive",
            "interactive": interactive,
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
                    f"Erro ao enviar interactive: {response.status_code} - {error_data}"
                )
                return {
                    "success": False,
                    "status_code": response.status_code,
                    "error": error_data,
                }

            result = response.json()
            message_id = result.get("messages", [{}])[0].get("id")
            logger.success(
                f"Interactive enviado | recipient={recipient} | message_id={message_id}"
            )
            return {
                "success": True,
                "message_id": message_id,
                "recipient": recipient,
            }


# Mapeamento de service_type para flow_id cadastrado na Meta
# Para adicionar novo flow: registrar no Meta, pegar o flow_id e adicionar aqui
FLOW_TEMPLATES = {
    "reparo_luminaria": "4141008006029185",
    "divida_ativa": "2093327131246166",
    # Adicionar novos flows aqui conforme forem criados na Meta
    # "poda_arvore": "FLOW_ID_AQUI",
    # "limpeza_urbana": "FLOW_ID_AQUI",
}


async def send_flow_by_service(
    service_type: str,
    user_number: str,
    flow_token: str | None = None,
    prefill_data: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """
    Dispara WhatsApp Flow apropriado para um tipo de serviço.

    Flows suportados (configurados em FLOW_TEMPLATES):
      - reparo_luminaria: Flow dinâmico de coleta de defeito de luminária
      - divida_ativa: Flow dinâmico de consulta de dívida ativa

    Args:
        service_type: Tipo de serviço ("reparo_luminaria" | "divida_ativa")
        user_number: Número do usuário no formato E.164 sem + (ex: 5521999999999)
        flow_token: Token opcional de rastreamento da sessão. Se ausente,
            gera um UUID. Vira a base (`_session`) do token v1:encoded
            enviado ao Meta quando há prefill.
        prefill_data: Entidades extraídas do contexto da conversa pelo
            agente, pra pré-preencher campos do formulário. Aceita qualquer
            JSON-serializable.

            Exemplos:
              - reparo_luminaria: {"defect_type": "Pendurada", "qty_pattern": "uma"}
              - divida_ativa: não usa prefill (dados coletados no flow)

            Encodado em `flow_token` (formato v1:base64) e decoded no
            endpoint MCP `_handle_init`. Chaves não-mapeadas pelo Flow JSON
            são ignoradas silenciosamente pelo cliente WhatsApp.

    Returns:
        Resultado do envio com message_id e flow_token. O flow_token
        retornado é o UUID BASE de correlação — NÃO o token v1:encoded (que
        pode conter PII como endereço e vai só pro payload do Meta). Evita
        propagar o payload reversível por callers / tool-results downstream.
    """
    flow_id = FLOW_TEMPLATES.get(service_type)

    if not flow_id:
        available = ", ".join(FLOW_TEMPLATES.keys())
        return {
            "success": False,
            "error": f"Flow não cadastrado para service_type='{service_type}'",
            "message": f"Flow disponível apenas para: {available}",
        }

    # UUID base de correlação cross-session (== `_session` dentro do token).
    base_token = flow_token or str(uuid.uuid4())

    # Normalização CENTRALIZADA: aplica per-service mapping de chaves +
    # valores (workflow internal → Flow JSON canonical IDs) antes do envio.
    # Garantee que TODOS os callers (explicit `send_whatsapp_flow` tool,
    # auto-flow no `multi_step_service`, ou wrapper `send_luminaria_flow`)
    # recebem a mesma normalização. Codex P2 round 5: explicit path estava
    # bypassing normalização e silenciosamente dropando keys workflow-shape.
    normalized_prefill = normalize_prefill_for_flow(service_type, prefill_data)

    # Encoda o prefill NO flow_token (v1:base64). O Flow é dinâmico
    # (data_api_version 3.0, restaurado 2026-05-20): o cliente WhatsApp
    # ignora `flow_action_payload.data` no INIT e o endpoint `_handle_init`
    # lê o prefill do flow_token decodificado (canal autoritativo). Sem
    # isso o formulário abre em branco mesmo com prefill conhecido. O
    # `flow_action_payload.data` (em send_flow) é mantido como fallback caso
    # o Flow volte a ser estático. encode_flow_token é no-op se prefill vazio.
    encoded_token = encode_flow_token(base_token, normalized_prefill)

    if normalized_prefill:
        # Log audit-friendly (keys only, sem valores — PII).
        logger.info(
            f"send_flow_by_service prefill_keys={sorted(normalized_prefill.keys())} "
            f"flow_token={_redact_flow_token(encoded_token)}"
        )

    sender = WhatsAppFlowSender()

    try:
        result = await sender.send_flow(
            recipient=user_number,
            flow_id=flow_id,
            flow_token=encoded_token,
            prefill_data=normalized_prefill or None,
        )

        # O token v1:encoded pode conter PII (ex: endereço) e vai SÓ pro
        # payload do Meta. Pra callers / tool-results (src/app.py propaga
        # flow_token sem redaction), retorna o UUID base de correlação —
        # nunca o payload reversível. Codex P2 2026-05-26.
        if isinstance(result, dict) and result.get("flow_token"):
            result["flow_token"] = base_token

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
    prefill_data: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Wrapper para compatibilidade. Use send_flow_by_service."""
    return await send_flow_by_service(
        "reparo_luminaria", user_number, flow_token, prefill_data
    )


async def send_divida_ativa_flow(
    user_number: str,
    flow_token: str | None = None,
    prefill_data: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Wrapper para envio do flow de Dívida Ativa. Use send_flow_by_service."""
    return await send_flow_by_service(
        "divida_ativa", user_number, flow_token, prefill_data
    )


async def send_interactive_envelope(
    user_number: str, interactive: Dict[str, Any]
) -> Dict[str, Any]:
    """Envia um envelope `interactive` (button/list) pro cidadão, best-effort.

    Wrapper estável pros callers (app.py): instancia o sender, captura falha
    de config/HTTP e SEMPRE retorna {"success": bool, ...} — nunca levanta, pra
    o caller cair em fallback de texto.
    """
    try:
        sender = WhatsAppFlowSender()
        return await sender.send_interactive(user_number, interactive)
    except Exception as e:
        logger.error(f"Erro ao enviar interactive envelope: {e}")
        return {"success": False, "error": str(e)}


async def render_interactive_confirm(
    interactive_spec: Optional[Dict[str, Any]],
    fallback_body: str,
    user_id: str,
    service_name: str,
) -> Optional[Dict[str, Any]]:
    """Camada-tool: monta o envelope da confirmação interativa (botões/lista),
    envia DIRETO pro cidadão e devolve a INSTRUÇÃO pro agente não duplicar em
    texto. Retorna None quando o caller deve cair no fallback de texto: spec
    ausente/inválido, sem buttons/sections, envelope inválido, ou envio falhou.

    Extraído do wrapper de app.py pra ser testável (o envio é mockável) — antes a
    orquestração vivia inline num closure não-coberto.
    """
    if not isinstance(interactive_spec, dict):
        return None

    from src.tools.whatsapp_interactive import (
        build_buttons_envelope,
        build_interactive_confirm_instruction,
        build_list_envelope,
    )

    body = interactive_spec.get("body") or fallback_body or ""
    if interactive_spec.get("buttons"):
        envelope = build_buttons_envelope(
            body=body, buttons=interactive_spec["buttons"]
        )
    elif interactive_spec.get("sections"):
        envelope = build_list_envelope(
            body=body,
            sections=interactive_spec["sections"],
            button_label=interactive_spec.get("button_label", "Ver opções"),
        )
    else:
        logger.warning(
            "[INTERACTIVE_CONFIRM] spec sem buttons/sections; fallback pra texto"
        )
        return None

    if envelope.get("status") != "ok":
        logger.warning(
            f"[INTERACTIVE_CONFIRM] envelope inválido ({envelope.get('error')}); "
            "fallback pra texto"
        )
        return None

    send_result = await send_interactive_envelope(user_id, envelope["interactive"])
    if not send_result.get("success"):
        logger.warning(
            f"[INTERACTIVE_CONFIRM] envio falhou ({send_result.get('error')}); "
            "fallback pra texto"
        )
        return None

    logger.info(
        f"[INTERACTIVE_CONFIRM] enviado | service={service_name} "
        f"| user={user_id} | msg_id={send_result.get('message_id')}"
    )
    return {
        "status": "interactive_sent",
        "next_step": "await_user_selection",
        "instruction": build_interactive_confirm_instruction(
            interactive_spec.get("field")
        ),
    }
