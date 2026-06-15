"""
Backend do WhatsApp Flow de consulta fiscal (Dívida Ativa).

Tipos de `action` recebidos:
  ping         → health check; retorna {"status": "active"}.
  INIT         → abre o flow na tela TIPO_CONSULTA com prefill opcional do flow_token.
  data_exchange → click em "Próximo"; trigger="tipo_consulta" → retorna tela
                  DADOS_CONSULTA configurada com label e visibilidade do campo "ano".
"""

from src.flows._crypto import _decrypt_request, _encrypt_response
from src.flows._token import decode_flow_token
from src.utils.log import logger


# Configuração por tipo: label do campo número + visibilidade/label do campo ano.
_TIPO_CONFIG: dict[str, dict] = {
    "cpf_cnpj": {
        "label_numero": "CPF/CNPJ",
        "show_ano": False,
        "label_ano": "",
    },
    "inscricao_imobiliaria": {
        "label_numero": "Insc. Imobiliária",
        "show_ano": False,
        "label_ano": "",
    },
    "auto_infracao": {
        "label_numero": "Auto de infração",
        "show_ano": True,
        "label_ano": "Ano do auto",
    },
    "cda": {
        "label_numero": "Número da CDA",
        "show_ano": False,
        "label_ano": "",
    },
    "execucao_fiscal": {
        "label_numero": "Número da EF",
        "show_ano": False,
        "label_ano": "",
    },
}


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _handle_init(
    incoming_data: dict | None = None, flow_token: str | None = None
) -> dict:
    """
    Abre o flow na tela TIPO_CONSULTA.

    Camadas de prefill (última ganha):
    1. Default vazio.
    2. incoming_data decriptado (vazio em Flow dinâmico hoje; defensive).
    3. flow_token decodificado — canal autoritativo do bot.
    """
    base_data: dict = {"tipo_consulta_prefill": ""}

    incoming = incoming_data or {}
    for key, value in incoming.items():
        base_data[key] = value

    token_data = decode_flow_token(flow_token)
    for key, value in token_data.items():
        if key.startswith("_"):
            continue
        canonical_key = key if key.endswith("_prefill") else f"{key}_prefill"
        if canonical_key in base_data:
            base_data[canonical_key] = value

    return {
        "version": "3.0",
        "screen": "TIPO_CONSULTA",
        "data": base_data,
    }


def _handle_tipo_consulta(tipo_consulta: str) -> dict:
    """
    User clicou "Próximo" com um tipo selecionado.
    Navega para DADOS_CONSULTA configurada com labels e visibilidade corretos.
    Fallback para cpf_cnpj em tipo desconhecido (defensive).
    """
    config = _TIPO_CONFIG.get(tipo_consulta) or _TIPO_CONFIG["cpf_cnpj"]
    logger.info(
        f"[DIVIDA_ATIVA] _handle_tipo_consulta: tipo={tipo_consulta!r} "
        f"show_ano={config['show_ano']}"
    )
    return {
        "version": "3.0",
        "screen": "DADOS_CONSULTA",
        "data": {
            "tipo_consulta": tipo_consulta,
            **config,
        },
    }


# ---------------------------------------------------------------------------
# Entry point público
# ---------------------------------------------------------------------------


async def process_flow_request(body: dict, private_key_pem: str) -> str:
    """
    Decripta o request do WhatsApp, processa a action e retorna a resposta
    criptografada pronta para ser devolvida ao WhatsApp.
    """
    try:
        payload, aes_key, iv = _decrypt_request(body, private_key_pem)
    except Exception as e:
        logger.error(f"divida_ativa: erro ao decriptar request: {e}")
        raise ValueError("Falha na decriptação do payload WhatsApp Flows") from e

    action = payload.get("action")
    data = payload.get("data", {})
    flow_token = payload.get("flow_token")
    logger.info(
        f"divida_ativa: action={action!r} data={data} "
        f"flow_token_present={bool(flow_token)}"
    )

    if action == "ping":
        response = {"data": {"status": "active"}}

    elif action == "INIT":
        response = _handle_init(data, flow_token=flow_token)

    elif action == "data_exchange":
        trigger = data.get("trigger")
        if trigger == "tipo_consulta":
            response = _handle_tipo_consulta(data.get("tipo_consulta", ""))
        else:
            logger.warning(f"divida_ativa: trigger desconhecido {trigger!r}")
            response = {"version": "3.0", "screen": "TIPO_CONSULTA", "data": {}}

    else:
        logger.warning(f"divida_ativa: action desconhecida {action!r}")
        response = {"version": "3.0", "screen": "TIPO_CONSULTA", "data": {}}

    return _encrypt_response(response, aes_key, iv)
