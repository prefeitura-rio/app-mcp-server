"""
Handler para WhatsApp Flow de Dívida Ativa (consulta).

Flow ID: 2093327131246166
Tipo: Dinâmico (data_api_version: 3.0)

Fluxo:
  1. TIPO_CONSULTA: usuário escolhe tipo (CPF/CNPJ, Inscrição, Auto, CDA, EF)
  2. data_exchange → este handler popula labels/visibilidade para próxima tela
  3. DADOS_CONSULTA: usuário preenche dados (ano opcional, número obrigatório)
  4. complete → payload enviado ao workflow divida_ativa

Protocolo de criptografia é o mesmo do luminaria_flow.
"""

import base64
import json
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from loguru import logger


def handle_divida_ativa_data_exchange(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Handler do data_exchange do flow de consulta de dívida ativa.

    Recebe trigger do TIPO_CONSULTA e retorna data para DADOS_CONSULTA com:
    - label_numero: label dinâmico do campo de número
    - show_ano: se deve mostrar campo de ano (True só para auto_infracao)
    - label_ano: label do campo de ano
    - tipo_consulta: echo do tipo escolhido (usado no complete)

    Args:
        payload: dict com trigger e tipo_consulta

    Returns:
        dict com screen data para DADOS_CONSULTA
    """
    trigger = payload.get("trigger")
    tipo_consulta = payload.get("tipo_consulta")

    logger.info(
        f"[divida_ativa_flow] data_exchange: trigger={trigger}, tipo={tipo_consulta}"
    )

    if trigger != "tipo_consulta":
        logger.warning(f"[divida_ativa_flow] trigger inesperado: {trigger}")
        return {
            "tipo_consulta": tipo_consulta or "",
            "label_numero": "Número",
            "show_ano": False,
            "label_ano": "Ano",
        }

    # Mapeamento de tipo → (label_numero, show_ano, label_ano)
    config = {
        "cpf_cnpj": ("CPF/CNPJ do contribuinte", False, ""),
        "inscricao_imobiliaria": ("Código da Inscrição Imobiliária", False, ""),
        "auto_infracao": (
            "Número do Auto de Infração",
            True,
            "Ano do Auto de Infração",
        ),
        "cda": ("Número da Certidão de Dívida Ativa (CDA)", False, ""),
        "execucao_fiscal": ("Número da Execução Fiscal (EF)", False, ""),
    }

    label_numero, show_ano, label_ano = config.get(tipo_consulta, ("Número", False, ""))

    response_data = {
        "tipo_consulta": tipo_consulta,
        "label_numero": label_numero,
        "show_ano": show_ano,
        "label_ano": label_ano if show_ano else "Ano",
    }

    logger.info(f"[divida_ativa_flow] response_data: {response_data}")
    return response_data


def normalize_flow_submission(flow_data: dict[str, Any]) -> dict[str, Any]:
    """
    Normaliza dados do flow para formato esperado pelo workflow.

    Mapeia:
      - tipo_consulta (flow) → consulta_debitos (workflow)
      - numero + tipo → campo específico (cpfCnpj, inscricaoImobiliaria, etc)
      - ano → anoAutoInfracao (se auto_infracao)

    Args:
        flow_data: payload do complete com _source, tipo_consulta, ano, numero

    Returns:
        dict normalizado para multi_step_service payload
    """
    tipo = flow_data.get("tipo_consulta", "")
    numero = flow_data.get("numero", "")
    ano = flow_data.get("ano", "")

    logger.info(
        f"[divida_ativa_flow] normalize: tipo={tipo}, numero={numero}, ano={ano}"
    )

    # Mapeamento tipo_consulta (flow) → consulta_debitos (workflow)
    tipo_map = {
        "cpf_cnpj": "cpfCnpj",
        "inscricao_imobiliaria": "inscricaoImobiliaria",
        "auto_infracao": "numeroAutoInfracao",
        "cda": "cda",
        "execucao_fiscal": "numeroExecucaoFiscal",
    }

    # Campo de valor para cada tipo
    valor_field_map = {
        "cpf_cnpj": "cpfCnpj",
        "inscricao_imobiliaria": "inscricaoImobiliaria",
        "auto_infracao": "numeroAutoInfracao",
        "cda": "cda",
        "execucao_fiscal": "numeroExecucaoFiscal",
    }

    consulta_tipo = tipo_map.get(tipo, tipo)
    valor_field = valor_field_map.get(tipo, "valor")

    normalized = {
        "_source": "whatsapp_flow",
        "consulta_debitos": consulta_tipo,
        valor_field: numero,
    }

    # Adiciona ano se for auto de infração
    if tipo == "auto_infracao" and ano:
        normalized["anoAutoInfracao"] = ano

    logger.info(f"[divida_ativa_flow] normalized payload: {normalized}")
    return normalized


# ---------------------------------------------------------------------------
# Crypto (mesmo protocolo do luminaria_flow)
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
    """Decripta request do WhatsApp Flow."""
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
    """Criptografa response do WhatsApp Flow."""
    flipped_iv = bytes(b ^ 0xFF for b in iv)
    encryptor = Cipher(algorithms.AES(aes_key), modes.GCM(flipped_iv)).encryptor()
    payload_bytes = json.dumps(data).encode("utf-8")
    encrypted = encryptor.update(payload_bytes) + encryptor.finalize()
    return base64.b64encode(encrypted + encryptor.tag).decode("utf-8")


async def process_divida_ativa_flow_request(body: dict, private_key_pem: str) -> bytes:
    """
    Processa request criptografado do WhatsApp Flow de Dívida Ativa.

    Actions suportadas:
      - INIT: retorna data vazio (não usa prefill)
      - data_exchange: popula labels dinâmicos baseado em tipo_consulta
      - ping: health check (retorna pong)

    Args:
        body: request body criptografado do Meta
        private_key_pem: chave privada RSA para decriptação

    Returns:
        response criptografado em bytes (formato exigido pelo Meta)

    Raises:
        ValueError: se decriptação falhar
    """
    try:
        payload, aes_key, iv = _decrypt_request(body, private_key_pem)
        logger.info(f"[divida_ativa_flow] decrypted payload: {payload}")
    except Exception as e:
        logger.error(f"[divida_ativa_flow] decryption error: {e}")
        raise ValueError(f"Decryptação falhou: {e}")

    action = payload.get("action")
    logger.info(f"[divida_ativa_flow] action: {action}")

    # Response base
    response = {"version": payload.get("version", "3.0")}

    if action == "ping":
        response["data"] = {"status": "active"}

    elif action == "INIT":
        # Flow não usa prefill - retorna data vazio
        response["data"] = {}

    elif action == "data_exchange":
        # Popula labels dinâmicos baseado em tipo_consulta
        exchange_data = handle_divida_ativa_data_exchange(payload.get("data", {}))
        response["data"] = exchange_data

    else:
        logger.warning(f"[divida_ativa_flow] unknown action: {action}")
        response["data"] = {}

    # Criptografa e retorna
    encrypted = _encrypt_response(response, aes_key, iv)
    return encrypted.encode("utf-8")
