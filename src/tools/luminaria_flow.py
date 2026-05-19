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


def _handle_init(incoming_data: dict | None = None) -> dict:
    """
    Resposta ao INIT request do WhatsApp Flow.

    `incoming_data` vem do `flow_action_payload.data` que o bot serializou
    no envio da mensagem Flow. Antes deste fix, `_handle_init` ignorava esse
    payload e sempre retornava `show_qty_pattern=False, show_location=False`,
    o que impedia prefill pra Flow dinâmico (`data_api_version: 3.0`).

    Comportamento agora:
    - Mantém defaults conservadores (`False`, `False`) se nada vier do bot.
    - Aceita override via payload pros campos hoje declarados no Flow JSON
      (`show_qty_pattern`, `show_location`).
    - Faz pass-through de keys adicionais que o bot envie — Meta ignora se
      não houver binding no Flow JSON; quando novas keys forem adicionadas
      ao schema (`endereco_prefill`, etc.), elas funcionam automaticamente
      sem novo deploy.
    """
    incoming = incoming_data or {}
    base_data = {
        "show_qty_pattern": False,
        "show_location": False,
    }
    # Override só pra keys conhecidas + propagação livre de keys extras.
    # Type-narrow show_* pra bool (Meta exige boolean estrito).
    for key in ("show_qty_pattern", "show_location"):
        if key in incoming:
            base_data[key] = bool(incoming[key])
    for key, value in incoming.items():
        if key not in base_data:
            base_data[key] = value

    return {
        "version": "3.0",
        "screen": "MAIN",
        "data": base_data,
    }


def _handle_defect_type(defect_type: str) -> dict:
    is_visual = defect_type in _VISUAL
    return {
        "version": "3.0",
        "screen": "MAIN",
        "data": {
            "show_qty_pattern": is_visual,
            "show_location": not is_visual,
        },
    }


def _handle_qty_pattern() -> dict:
    return {
        "version": "3.0",
        "screen": "MAIN",
        "data": {
            "show_qty_pattern": True,
            "show_location": True,
        },
    }


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
    logger.info(f"luminaria_flow: action={action!r} data={data}")

    if action == "ping":
        response = {"data": {"status": "active"}}

    elif action == "INIT":
        response = _handle_init(data)

    elif action == "data_exchange":
        trigger = data.get("trigger")
        if trigger == "defect_type":
            response = _handle_defect_type(data.get("defect_type", ""))
        elif trigger == "qty_pattern":
            response = _handle_qty_pattern()
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
