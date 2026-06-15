"""
Helpers que constroem o objeto `interactive` Meta WhatsApp Business
sem que o LLM precise saber o schema completo. Usados pelos tools
`send_whatsapp_flow`, `send_whatsapp_buttons`, `send_whatsapp_list`
em app.py — cada um delega aqui pra montar a estrutura.

Cada função retorna o envelope canônico `{status, type: "interactive",
interactive: {...}}` consumido pelo Mule (`vars.agentMedia` em
webhook-flow.xml, ADR-022).

Schema Meta API ref:
https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages#interactive-object

ADR-022, ADR-024.
"""

from __future__ import annotations

from typing import Any, Optional

# Limite Meta: header text até 60 chars, body até 1024, footer até 60,
# button title até 20, list section title até 24, row title até 24,
# row description até 72. Validamos os mais críticos pra evitar erro
# 100 do Meta.
_HEADER_MAX = 60
_BODY_MAX = 1024
_FOOTER_MAX = 60
_BUTTON_TITLE_MAX = 20
_BUTTON_ID_MAX = 256
_LIST_SECTION_TITLE_MAX = 24
_LIST_ROW_TITLE_MAX = 24
_LIST_ROW_DESC_MAX = 72
_LIST_BUTTON_MAX = 20  # "Veja opções"
_CTA_URL_DISPLAY_MAX = 20  # "Entrar no Gov.BR"


def _err(msg: str) -> dict[str, Any]:
    return {"status": "error", "error": msg}


def encode_prefill_token(
    flow_token: str,
    prefill_data: Optional[dict[str, Any]] = None,
    service_type: Optional[str] = None,
) -> str:
    """Encoda prefill no ``flow_token`` pra Flow dinâmico (data_api_version 3.0).

    O canal de prefill de um Flow dinâmico é o ``flow_token``: o endpoint
    ``_handle_init`` decoda os valores e os devolve no INIT response, abrindo o
    formulário já pré-preenchido. ``service_type`` é OBRIGATÓRIO pra prefilar: o
    normalizer do serviço mapeia os valores pros IDs canônicos do Flow (ex:
    ``"apagada"`` → ``"Apagada"``) e — crucial — whitelista só os campos do
    Flow, descartando qualquer chave fora dele (inclusive PII como CPF/endereço,
    que NÃO deve ir no token).

    Sem prefill, sem ``service_type``, ou após normalização vazia retorna o
    ``flow_token`` original intacto (UUID opaco) — back-compat.
    """
    if not prefill_data or not service_type:
        return flow_token
    from src.flows._token import encode_flow_token
    from src.tools.whatsapp_flows.normalizers import normalize_prefill_for_flow

    normalized = normalize_prefill_for_flow(service_type, prefill_data)
    if not normalized:
        return flow_token
    return encode_flow_token(flow_token, normalized)


def build_flow_envelope(
    flow_id: str,
    body: str,
    flow_token: str,
    cta: str = "Abrir formulário",
    header: Optional[str] = None,
    footer: Optional[str] = None,
    flow_action: str = "navigate",
    flow_action_payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Constrói envelope pra enviar WhatsApp Flow PRO cidadão.

    Args:
        flow_id:    ID do Flow registrado no Meta Business Manager.
        body:       Texto do corpo da mensagem (≤1024 chars).
        flow_token: Token gerado pelo bot pra correlacionar a resposta
                    do cidadão. Pode ser UUID; será devolvido no
                    nfm_reply quando o cidadão submeter.
        cta:        Texto do botão que abre o Flow (≤20 chars).
        header:     Header opcional (≤60 chars).
        footer:     Footer opcional (≤60 chars).
        flow_action: "navigate" (default — abre na screen inicial) ou
                    "data_exchange" (Flow Endpoint custom).
        flow_action_payload: Para "navigate" usa
                    `{"screen": "<SCREEN_ID>", "data": {...}}`. Default
                    "MAIN" (tela de entrada do Flow reparo_luminaria) sem data.

    Retorna `{status, type: "interactive", interactive: {...}}`.
    """
    if not flow_id:
        return _err("flow_id obrigatório (do Meta Business Manager).")
    if not body:
        return _err("body obrigatório (texto do corpo da mensagem).")
    if len(body) > _BODY_MAX:
        return _err(f"body excede {_BODY_MAX} chars (Meta limit).")
    if not flow_token:
        return _err(
            "flow_token obrigatório (UUID gerado pelo bot pra correlacionar resposta)."
        )
    if len(cta) > _BUTTON_TITLE_MAX:
        return _err(f"cta excede {_BUTTON_TITLE_MAX} chars (Meta limit).")
    if header and len(header) > _HEADER_MAX:
        return _err(f"header excede {_HEADER_MAX} chars.")
    if footer and len(footer) > _FOOTER_MAX:
        return _err(f"footer excede {_FOOTER_MAX} chars.")
    if flow_action not in ("navigate", "data_exchange"):
        return _err(
            f"flow_action inválido: '{flow_action}'. "
            "Permitidos: 'navigate' (default) ou 'data_exchange'."
        )

    # Default screen = "MAIN" (tela de entrada do único Flow registrado, reparo_luminaria).
    # NÃO usar "FIRST_SCREEN" (placeholder genérico): o Meta rejeita screen inexistente com
    # 400 BAD_REQUEST e o card NÃO chega ao cidadão (validado 2026-06-03; era o motivo de o
    # Flow não aparecer). Flow novo com outra tela inicial precisa passar
    # flow_action_payload={"screen": "<entry_screen>"} explicitamente.
    payload: dict[str, Any] = flow_action_payload or {"screen": "MAIN"}

    interactive: dict[str, Any] = {
        "type": "flow",
        "body": {"text": body},
        "action": {
            "name": "flow",
            "parameters": {
                "flow_message_version": "3",
                "flow_token": flow_token,
                "flow_id": flow_id,
                "flow_cta": cta,
                "flow_action": flow_action,
                "flow_action_payload": payload,
            },
        },
    }
    if header:
        interactive["header"] = {"type": "text", "text": header}
    if footer:
        interactive["footer"] = {"text": footer}

    return {"status": "ok", "type": "interactive", "interactive": interactive}


def build_buttons_envelope(
    body: str,
    buttons: list[dict[str, str]],
    header: Optional[str] = None,
    footer: Optional[str] = None,
) -> dict[str, Any]:
    """Constrói envelope pra interactive buttons (até 3).

    Args:
        body:    Texto do corpo (≤1024 chars).
        buttons: Lista de até 3 botões. Cada botão é
                 `{"id": "<reply_id>", "title": "<rotulo>"}`.
                 `id` é o que volta como `button_reply.id` no inbound
                 (use snake_case curto), `title` é o que o cidadão vê.
        header:  Header opcional.
        footer:  Footer opcional.
    """
    if not body:
        return _err("body obrigatório.")
    if len(body) > _BODY_MAX:
        return _err(f"body excede {_BODY_MAX} chars.")
    if not buttons:
        return _err("buttons obrigatório (lista não-vazia, até 3 botões).")
    if len(buttons) > 3:
        return _err(f"buttons máx 3 (Meta limit). Recebido: {len(buttons)}.")
    if header and len(header) > _HEADER_MAX:
        return _err(f"header excede {_HEADER_MAX} chars.")
    if footer and len(footer) > _FOOTER_MAX:
        return _err(f"footer excede {_FOOTER_MAX} chars.")

    button_objs: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for i, b in enumerate(buttons):
        if not isinstance(b, dict):
            return _err(f"buttons[{i}] não é objeto.")
        bid = (b.get("id") or "").strip()
        title = (b.get("title") or "").strip()
        if not bid:
            return _err(f"buttons[{i}].id vazio.")
        if not title:
            return _err(f"buttons[{i}].title vazio.")
        if len(bid) > _BUTTON_ID_MAX:
            return _err(f"buttons[{i}].id excede {_BUTTON_ID_MAX} chars.")
        if len(title) > _BUTTON_TITLE_MAX:
            return _err(f"buttons[{i}].title excede {_BUTTON_TITLE_MAX} chars.")
        if bid in seen_ids:
            return _err(f"buttons[{i}].id duplicado: '{bid}'.")
        seen_ids.add(bid)
        button_objs.append({"type": "reply", "reply": {"id": bid, "title": title}})

    interactive: dict[str, Any] = {
        "type": "button",
        "body": {"text": body},
        "action": {"buttons": button_objs},
    }
    if header:
        interactive["header"] = {"type": "text", "text": header}
    if footer:
        interactive["footer"] = {"text": footer}

    return {"status": "ok", "type": "interactive", "interactive": interactive}


def build_list_envelope(
    body: str,
    sections: list[dict[str, Any]],
    button_label: str = "Ver opções",
    header: Optional[str] = None,
    footer: Optional[str] = None,
) -> dict[str, Any]:
    """Constrói envelope pra interactive list (até 10 sections × 10 rows).

    Args:
        body:         Texto do corpo (≤1024 chars).
        sections:     Lista de seções. Cada seção:
                      `{"title": "<nome>", "rows": [{"id": "...",
                                                      "title": "...",
                                                      "description": "..."}]}`.
                      Máx 10 seções, máx 10 rows total, row.title ≤24,
                      row.description ≤72.
        button_label: Rótulo do botão que abre a lista (≤20 chars).
        header:       Header opcional.
        footer:       Footer opcional.
    """
    if not body:
        return _err("body obrigatório.")
    if len(body) > _BODY_MAX:
        return _err(f"body excede {_BODY_MAX} chars.")
    if not sections:
        return _err("sections obrigatório (lista não-vazia).")
    if len(sections) > 10:
        return _err(f"sections máx 10 (Meta limit). Recebido: {len(sections)}.")
    if len(button_label) > _LIST_BUTTON_MAX:
        return _err(f"button_label excede {_LIST_BUTTON_MAX} chars.")
    if header and len(header) > _HEADER_MAX:
        return _err(f"header excede {_HEADER_MAX} chars.")
    if footer and len(footer) > _FOOTER_MAX:
        return _err(f"footer excede {_FOOTER_MAX} chars.")

    section_objs: list[dict[str, Any]] = []
    total_rows = 0
    seen_row_ids: set[str] = set()
    for si, s in enumerate(sections):
        if not isinstance(s, dict):
            return _err(f"sections[{si}] não é objeto.")
        s_title = (s.get("title") or "").strip()
        rows = s.get("rows") or []
        if not s_title:
            return _err(f"sections[{si}].title vazio.")
        if len(s_title) > _LIST_SECTION_TITLE_MAX:
            return _err(f"sections[{si}].title excede {_LIST_SECTION_TITLE_MAX} chars.")
        if not rows:
            return _err(f"sections[{si}].rows vazio.")
        row_objs: list[dict[str, Any]] = []
        for ri, r in enumerate(rows):
            if not isinstance(r, dict):
                return _err(f"sections[{si}].rows[{ri}] não é objeto.")
            rid = (r.get("id") or "").strip()
            rtitle = (r.get("title") or "").strip()
            rdesc = (r.get("description") or "").strip()
            if not rid:
                return _err(f"sections[{si}].rows[{ri}].id vazio.")
            if not rtitle:
                return _err(f"sections[{si}].rows[{ri}].title vazio.")
            if len(rtitle) > _LIST_ROW_TITLE_MAX:
                return _err(
                    f"sections[{si}].rows[{ri}].title excede {_LIST_ROW_TITLE_MAX} chars."
                )
            if rdesc and len(rdesc) > _LIST_ROW_DESC_MAX:
                return _err(
                    f"sections[{si}].rows[{ri}].description excede {_LIST_ROW_DESC_MAX} chars."
                )
            if rid in seen_row_ids:
                return _err(f"id de row duplicado: '{rid}'.")
            seen_row_ids.add(rid)
            row_obj: dict[str, Any] = {"id": rid, "title": rtitle}
            if rdesc:
                row_obj["description"] = rdesc
            row_objs.append(row_obj)
            total_rows += 1
        section_objs.append({"title": s_title, "rows": row_objs})

    if total_rows > 10:
        return _err(
            f"Total de rows ({total_rows}) excede limite Meta (10). "
            "Distribua menos opções ou separe em múltiplas mensagens."
        )

    interactive: dict[str, Any] = {
        "type": "list",
        "body": {"text": body},
        "action": {"button": button_label, "sections": section_objs},
    }
    if header:
        interactive["header"] = {"type": "text", "text": header}
    if footer:
        interactive["footer"] = {"text": footer}

    return {"status": "ok", "type": "interactive", "interactive": interactive}


def build_cta_url_envelope(
    body: str,
    url: str,
    display_text: str = "Abrir link",
    header: Optional[str] = None,
    footer: Optional[str] = None,
) -> dict[str, Any]:
    """Constrói envelope pra CTA URL button (botão verde com link externo).

    Args:
        body:         Texto do corpo da mensagem (≤1024 chars).
        url:          URL completa que o botão abre (deve começar com https://).
        display_text: Texto exibido no botão (≤20 chars).
        header:       Header opcional (≤60 chars).
        footer:       Footer opcional (≤60 chars).

    Retorna `{status, type: "interactive", interactive: {...}}`.
    """
    if not body:
        return _err("body obrigatório.")
    if len(body) > _BODY_MAX:
        return _err(f"body excede {_BODY_MAX} chars.")
    if not url:
        return _err("url obrigatório.")
    if not url.startswith("https://"):
        return _err("url deve começar com https:// (Meta requer HTTPS).")
    if not display_text:
        return _err("display_text obrigatório (texto do botão).")
    if len(display_text) > _CTA_URL_DISPLAY_MAX:
        return _err(f"display_text excede {_CTA_URL_DISPLAY_MAX} chars.")
    if header and len(header) > _HEADER_MAX:
        return _err(f"header excede {_HEADER_MAX} chars.")
    if footer and len(footer) > _FOOTER_MAX:
        return _err(f"footer excede {_FOOTER_MAX} chars.")

    interactive: dict[str, Any] = {
        "type": "cta_url",
        "body": {"text": body},
        "action": {
            "name": "cta_url",
            "parameters": {
                "display_text": display_text,
                "url": url,
            },
        },
    }
    if header:
        interactive["header"] = {"type": "text", "text": header}
    if footer:
        interactive["footer"] = {"text": footer}

    return {"status": "ok", "type": "interactive", "interactive": interactive}
