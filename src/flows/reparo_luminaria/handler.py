"""
Backend do WhatsApp Flow de coleta de defeito de luminária pública.

Tipos de `action` recebidos:
  INIT         → abre o flow; retorna tela MAIN com visibilidade zerada.
  data_exchange → seleção de campo; retorna novos booleans de visibilidade.
"""

from src.flows._crypto import _decrypt_request, _encrypt_response
from src.flows._token import decode_flow_token
from src.utils.log import logger

# Tipos visuais: mostram pergunta 2 (qty_pattern)
_VISUAL = {"Apagada", "Piscando", "Acesa de dia"}

_CLASSIFICATION = {
    ("Apagada", "uma"): "Apagada",
    ("Apagada", "bloco"): "Bloco ou grupo de luminárias apagadas",
    ("Apagada", "intercaladas"): "Várias luminárias intercaladas apagadas",
    ("Piscando", "uma"): "Piscando",
    ("Piscando", "bloco"): "Bloco ou grupo de luminárias piscando",
    ("Piscando", "intercaladas"): "Bloco ou grupo de luminárias piscando",
    ("Acesa de dia", "uma"): "Acesa durante o dia",
    ("Acesa de dia", "bloco"): "Bloco ou grupo de luminárias acesas de dia",
    ("Acesa de dia", "intercaladas"): "Várias luminárias intercaladas acesas de dia",
    ("Pendurada", ""): "Pendurada",
    ("Danificada", ""): "Danificada",
    ("Com ruído", ""): "Com ruído",
}


# ---------------------------------------------------------------------------
# Handlers de lógica
# ---------------------------------------------------------------------------


def _compute_visibility(
    defect_type: str | None, qty_pattern: str | None
) -> tuple[bool, bool]:
    """
    Visibilidade de `qty_pattern` + `location` no Flow — SEMPRE visíveis.

    2026-06-03: antes era disclosure progressivo (qty só pra defeito visual;
    location só após qty ser selecionada, via data_exchange). Resultado: campos
    NÃO-prefillados ficavam ESCONDIDOS no formulário e o workflow os coletava por
    TEXTO depois do submit — UX desconexa. Agora os dois ficam sempre visíveis.
    Params mantidos por back-compat de assinatura.
    """
    return True, True


def _handle_init(
    incoming_data: dict | None = None, flow_token: str | None = None
) -> dict:
    """
    Resposta ao INIT request do WhatsApp Flow.

    Compõe `data` da response em camadas (última ganha):
    1. Defaults conservadores (`show_*=False`, prefills=None).
    2. `incoming_data` decriptado (vazio em Flow dinâmico hoje; defensive).
    3. Prefill do `flow_token` decodificado via `decode_flow_token` (v1:base64).
    4. Smart visibility: `show_qty_pattern` e `show_location` calculados
       da combinação `defect_type` + `qty_pattern` resolvidos acima.
    """
    base_data: dict = {
        "defect_type_prefill": None,
        "qty_pattern_prefill": None,
        "location_prefill": None,
        "show_qty_pattern": False,
        "show_location": False,
        "show_quadra_question": False,
    }

    incoming = incoming_data or {}
    for key, value in incoming.items():
        base_data[key] = value

    token_data = decode_flow_token(flow_token)
    for key, value in token_data.items():
        if key.startswith("_"):
            continue
        canonical_key = (
            key
            if key.endswith("_prefill") or key.startswith("show_")
            else f"{key}_prefill"
        )
        if canonical_key in ("show_qty_pattern", "show_location"):
            base_data[canonical_key] = bool(value)
        else:
            base_data[canonical_key] = value

    explicit_show = (
        "show_qty_pattern" in incoming
        or "show_qty_pattern" in token_data
        or "show_location" in incoming
        or "show_location" in token_data
    )
    if not explicit_show:
        show_qty, show_loc = _compute_visibility(
            base_data.get("defect_type_prefill"),
            base_data.get("qty_pattern_prefill"),
        )
        base_data["show_qty_pattern"] = show_qty
        base_data["show_location"] = show_loc

    return {
        "version": "3.0",
        "screen": "MAIN",
        "data": base_data,
    }


def _preserved_prefills(flow_token: str | None) -> dict:
    """
    Decoda flow_token pra recuperar prefills ORIGINAIS enviados pelo bot.
    Sem isso, data_exchange handlers perderiam contexto que o bot já conhecia.
    """
    token_data = decode_flow_token(flow_token)
    out = {}
    for key in (
        "defect_type_prefill",
        "qty_pattern_prefill",
        "location_prefill",
    ):
        canonical = token_data.get(key)
        if canonical is None:
            alias_key = key[: -len("_prefill")]
            canonical = token_data.get(alias_key)
        if canonical is not None:
            out[key] = canonical
    return out


def _merge_current_form_state(incoming: dict, flow_token: str | None) -> dict:
    """
    Compõe prefills usando CURRENT form state (do incoming payload) como
    source of truth, com fallback pro token pra campos que user ainda não
    interagiu. Nunca reverte seleções do usuário.
    """
    out = _preserved_prefills(flow_token)
    for src_key, dst_key in (
        ("defect_type", "defect_type_prefill"),
        ("qty_pattern", "qty_pattern_prefill"),
        ("location", "location_prefill"),
        ("is_quadra_esportes", "is_quadra_esportes"),
    ):
        value = incoming.get(src_key)
        if value:
            out[dst_key] = value
    return out


def _handle_defect_type(
    defect_type: str,
    incoming: dict | None = None,
    flow_token: str | None = None,
) -> dict:
    incoming = dict(incoming or {})
    incoming["defect_type"] = defect_type
    data = {
        **_merge_current_form_state(incoming, flow_token),
        "show_qty_pattern": True,
        "show_location": True,
    }
    return {"version": "3.0", "screen": "MAIN", "data": data}


def _handle_qty_pattern(
    qty_pattern: str = "",
    incoming: dict | None = None,
    flow_token: str | None = None,
) -> dict:
    incoming = dict(incoming or {})
    incoming["qty_pattern"] = qty_pattern
    data = {
        **_merge_current_form_state(incoming, flow_token),
        "show_qty_pattern": True,
        "show_location": True,
        "show_quadra_question": False,
    }
    return {"version": "3.0", "screen": "MAIN", "data": data}


def _handle_location(
    location: str = "",
    incoming: dict | None = None,
    flow_token: str | None = None,
) -> dict:
    incoming = dict(incoming or {})
    incoming["location"] = location
    show_quadra = location == "Praça"
    logger.info(
        f"[REPARO_LUMINARIA] _handle_location: location={location!r}, "
        f"show_quadra={show_quadra}"
    )
    data = {
        **_merge_current_form_state(incoming, flow_token),
        "show_qty_pattern": True,
        "show_location": True,
        "show_quadra_question": show_quadra,
    }
    return {"version": "3.0", "screen": "MAIN", "data": data}


def _classify(defect_type: str, qty_pattern: str) -> str:
    key = (defect_type, qty_pattern or "")
    return _CLASSIFICATION.get(key, defect_type)


# ---------------------------------------------------------------------------
# Entry point público
# ---------------------------------------------------------------------------


async def process_flow_request(body: dict, private_key_pem: str) -> str:
    """
    Decripta o request do WhatsApp, processa a action e retorna a resposta
    criptografada (bytes) pronta para ser devolvida ao WhatsApp.
    """
    try:
        payload, aes_key, iv = _decrypt_request(body, private_key_pem)
    except Exception as e:
        logger.error(f"reparo_luminaria: erro ao decriptar request: {e}")
        raise ValueError("Falha na decriptação do payload WhatsApp Flows") from e

    action = payload.get("action")
    data = payload.get("data", {})
    flow_token = payload.get("flow_token")
    logger.info(
        f"reparo_luminaria: action={action!r} data={data} "
        f"flow_token_present={bool(flow_token)} "
        f"flow_token_v1={isinstance(flow_token, str) and flow_token.startswith('v1:')}"
    )

    if action == "ping":
        response = {"data": {"status": "active"}}

    elif action == "INIT":
        response = _handle_init(data, flow_token=flow_token)

    elif action == "data_exchange":
        trigger = data.get("trigger")
        if trigger == "defect_type":
            response = _handle_defect_type(
                data.get("defect_type", ""), incoming=data, flow_token=flow_token
            )
        elif trigger == "qty_pattern":
            response = _handle_qty_pattern(
                data.get("qty_pattern", ""), incoming=data, flow_token=flow_token
            )
        elif trigger == "location":
            response = _handle_location(
                data.get("location", ""), incoming=data, flow_token=flow_token
            )
        else:
            logger.warning(f"reparo_luminaria: trigger desconhecido {trigger!r}")
            response = {"version": "3.0", "data": {}}

    else:
        logger.warning(f"reparo_luminaria: action desconhecida {action!r}")
        response = {"version": "3.0", "data": {}}

    return _encrypt_response(response, aes_key, iv)


def classify_defect(defect_type: str, qty_pattern: str) -> str:
    """Utilitário exposto para o agente classificar após o flow completar."""
    return _classify(defect_type, qty_pattern)
