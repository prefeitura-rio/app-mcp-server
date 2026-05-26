"""
Backend do WhatsApp Flow de coleta de defeito de luminária pública.

Protocolo de criptografia WhatsApp Flows:
  Request  → RSA-OAEP(SHA-256) decripta a AES key; AES-GCM decripta o payload.
  Response → AES-GCM criptografa a resposta com novo IV aleatório.

Tipos de `action` recebidos:
  INIT         → abre o flow; retorna tela MAIN com visibilidade zerada.
  data_exchange → seleção de campo; retorna novos booleans de visibilidade.
"""

import base64
import json

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from src.tools.luminaria_entity_extractor import decode_flow_token
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
# Crypto
# ---------------------------------------------------------------------------


def _normalize_pem(pem: str) -> str:
    """Reconstrói PEM com quebras de linha caso a env var não as preserve."""
    if "\n" in pem:
        return pem
    header = "-----BEGIN PRIVATE KEY-----"
    footer = "-----END PRIVATE KEY-----"
    raw = pem.replace(header, "").replace(footer, "").strip()
    wrapped = "\n".join(raw[i : i + 64] for i in range(0, len(raw), 64))
    return f"{header}\n{wrapped}\n{footer}\n"


def _decrypt_request(body: dict, private_key_pem: str):
    private_key = serialization.load_pem_private_key(
        _normalize_pem(private_key_pem).encode(), password=None
    )
    aes_key = private_key.decrypt(
        base64.b64decode(body["encrypted_aes_key"]),
        asym_padding.OAEP(
            mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    iv = base64.b64decode(body["initial_vector"])
    flow_data = base64.b64decode(body["encrypted_flow_data"])
    encrypted_body, tag = flow_data[:-16], flow_data[-16:]
    decryptor = Cipher(algorithms.AES(aes_key), modes.GCM(iv, tag)).decryptor()
    payload_bytes = decryptor.update(encrypted_body) + decryptor.finalize()
    return json.loads(payload_bytes), aes_key, iv


def _encrypt_response(data: dict, aes_key: bytes, iv: bytes) -> str:
    flipped_iv = bytes(b ^ 0xFF for b in iv)
    encryptor = Cipher(algorithms.AES(aes_key), modes.GCM(flipped_iv)).encryptor()
    payload_bytes = json.dumps(data).encode("utf-8")
    encrypted = encryptor.update(payload_bytes) + encryptor.finalize()
    return base64.b64encode(encrypted + encryptor.tag).decode("utf-8")


# ---------------------------------------------------------------------------
# Handlers de lógica
# ---------------------------------------------------------------------------


def _compute_visibility(
    defect_type: str | None, qty_pattern: str | None
) -> tuple[bool, bool]:
    """
    Smart visibility — replica a lógica original do `_handle_defect_type`
    (transição via data_exchange) mas aplicada já no INIT quando temos
    prefill de `defect_type`.

    - Defeito visual (Apagada/Piscando/Acesa de dia): mostra qty_pattern,
      esconde location (location aparece quando qty_pattern selecionada).
    - Defeito não-visual (Pendurada/Danificada/Com ruído): vai direto
      pra location.
    - Sem defect_type: tudo escondido (defaults).
    - Visual + qty_pattern já selecionada: mostra ambos (transição completa).
    """
    if not defect_type:
        return False, False
    is_visual = defect_type in _VISUAL
    has_qty = bool(qty_pattern)
    show_qty_pattern = is_visual
    show_location = (not is_visual) or has_qty
    return show_qty_pattern, show_location


def _handle_init(
    incoming_data: dict | None = None, flow_token: str | None = None
) -> dict:
    """
    Resposta ao INIT request do WhatsApp Flow.

    Compõe `data` da response em camadas (última ganha):
    1. Defaults conservadores (`show_*=False`, prefills=None).
    2. `incoming_data` decriptado (vazio em Flow dinâmico hoje; defensive).
    3. Prefill do `flow_token` decodificado via `decode_flow_token`
       (v1:base64). Canal autoritativo pra Flow dinâmico.
    4. Smart visibility: `show_qty_pattern` e `show_location` calculados
       da combinação `defect_type` + `qty_pattern` resolvidos acima
       (replica lógica do data_exchange original).
    """
    # Layer 1: defaults
    base_data: dict = {
        "defect_type_prefill": None,
        "qty_pattern_prefill": None,
        "location_prefill": None,
        "show_qty_pattern": False,
        "show_location": False,
    }

    # Layer 2: incoming_data decriptado (futuro-proof; vazio em dinâmico hoje)
    incoming = incoming_data or {}
    for key, value in incoming.items():
        base_data[key] = value

    # Layer 3: flow_token decodificado (canal real pra prefill)
    token_data = decode_flow_token(flow_token)
    for key, value in token_data.items():
        # `_session` é metadata de correlação inserida por `encode_flow_token`
        # — não é prefill renderizável; mantém apenas pra audit se quiser.
        if key.startswith("_"):
            continue
        # Aceita alias sem `_prefill` (compat com bot que envia
        # `defect_type=X` direto) — normaliza pra key canônica.
        canonical_key = (
            key
            if key.endswith("_prefill") or key.startswith("show_")
            else f"{key}_prefill"
        )
        if canonical_key in ("show_qty_pattern", "show_location"):
            base_data[canonical_key] = bool(value)
        else:
            base_data[canonical_key] = value

    # Layer 4: smart visibility derivada dos prefills resolvidos.
    # Só aplica se prefill veio (não sobrescreve override explícito do bot
    # em incoming_data/token; mas se nenhum show_* foi setado, computa).
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
    Decoda flow_token pra recuperar prefills ORIGINAIS sent by bot
    (Engine via send_flow_by_service). Sem isso, data_exchange handlers
    perderiam contexto que o bot já conhecia (e.g. bot sabia location
    da conversa anterior, mas user troca defect_type → endpoint precisa
    PRESERVAR location_prefill que o bot mandou).

    Princípio: data_exchange handlers update apenas o campo que mudou
    + visibility flags. Outros prefills vêm do token (canal autoritativo
    do bot pra dados conhecidos da conversa).
    """
    token_data = decode_flow_token(flow_token)
    out = {}
    for key in (
        "defect_type_prefill",
        "qty_pattern_prefill",
        "location_prefill",
    ):
        # Aceita ambas keys: canonical "X_prefill" e alias "X" (bot pode mandar qualquer)
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
    interagiu.

    Bug histórico: handler usava só token → reverter user's selections em
    transições subsequentes. Fix: Meta on-select-action payload envia
    TODOS os ${form.X}, então `incoming` tem estado atual. Token só
    contribui pra campos que estão ausentes/vazios no incoming.

    Princípio padrão pra Flows dinâmicos: never revert user input.
    """
    out = _preserved_prefills(flow_token)
    for src_key, dst_key in (
        ("defect_type", "defect_type_prefill"),
        ("qty_pattern", "qty_pattern_prefill"),
        ("location", "location_prefill"),
    ):
        value = incoming.get(src_key)
        if value:  # non-empty/truthy — user filled
            out[dst_key] = value
    return out


def _handle_defect_type(
    defect_type: str,
    incoming: dict | None = None,
    flow_token: str | None = None,
) -> dict:
    """
    User selecionou novo defect_type. Echo TODO form state atual +
    visibility flags.

    on-select-action payload envia defect_type + qty_pattern + location
    do form atual. Handler ecoa tudo pra evitar revert em transições
    subsequentes.
    """
    is_visual = defect_type in _VISUAL
    incoming = dict(incoming or {})
    incoming["defect_type"] = defect_type
    data = {
        **_merge_current_form_state(incoming, flow_token),
        "show_qty_pattern": is_visual,
        "show_location": not is_visual,
    }
    return {"version": "3.0", "screen": "MAIN", "data": data}


def _handle_qty_pattern(
    qty_pattern: str = "",
    incoming: dict | None = None,
    flow_token: str | None = None,
) -> dict:
    """
    User selecionou qty_pattern. Mesma lógica do _handle_defect_type:
    echo TODO estado atual + visibility flags. Não revert nada.
    """
    incoming = dict(incoming or {})
    incoming["qty_pattern"] = qty_pattern
    data = {
        **_merge_current_form_state(incoming, flow_token),
        "show_qty_pattern": True,
        "show_location": True,
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
        logger.error(f"luminaria_flow: erro ao decriptar request: {e}")
        raise ValueError("Falha na decriptação do payload WhatsApp Flows") from e

    action = payload.get("action")
    data = payload.get("data", {})
    flow_token = payload.get("flow_token")
    # Log tipos pra debug — não logar valor do token (pode conter PII em
    # base64 ou pelo menos ser sensível por correlação com a session).
    logger.info(
        f"luminaria_flow: action={action!r} data={data} "
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
        else:
            logger.warning(f"luminaria_flow: trigger desconhecido {trigger!r}")
            response = {"version": "3.0", "data": {}}

    else:
        logger.warning(f"luminaria_flow: action desconhecida {action!r}")
        response = {"version": "3.0", "data": {}}

    return _encrypt_response(response, aes_key, iv)


def classify_defect(defect_type: str, qty_pattern: str) -> str:
    """Utilitário exposto para o agente classificar após o flow completar."""
    return _classify(defect_type, qty_pattern)
