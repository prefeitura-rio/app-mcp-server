"""
Crypto compartilhado para WhatsApp Flows.

  Request  → RSA-OAEP(SHA-256) decripta a AES key; AES-GCM decripta o payload.
  Response → AES-GCM criptografa com IV flipado (todos os bits XOR 0xFF).
"""

import base64
import json

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


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
    """Retorna (payload_dict, aes_key, iv)."""
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
    """Retorna resposta criptografada em base64 (text/plain pro Meta)."""
    flipped_iv = bytes(b ^ 0xFF for b in iv)
    encryptor = Cipher(algorithms.AES(aes_key), modes.GCM(flipped_iv)).encryptor()
    payload_bytes = json.dumps(data).encode("utf-8")
    encrypted = encryptor.update(payload_bytes) + encryptor.finalize()
    return base64.b64encode(encrypted + encryptor.tag).decode("utf-8")
