"""
Backend do WhatsApp Flow de opções de dívida ativa.

A tela OPCOES é terminal (complete). O servidor só é chamado para:
  ping    → health check
  INIT    → retorna o array `opcoes` filtrado pelo contexto do flow_token

Use `build_opcoes_flow_token` para montar o flow_token antes de enviar o flow:

    from src.flows.divida_ativa.opcoes.handler import build_opcoes_flow_token

    flow_token = build_opcoes_flow_token(
        session_uuid,
        total_nao_parcelado=resultado.total_nao_parcelado,
        total_parcelado=resultado.total_parcelado,
    )

Nota: o preview do Meta Flow Builder exibe o `__example__` do flow.json, não
o array retornado pelo servidor. O array dinâmico é usado em produção no
WhatsApp real. Conforme documentação Meta:
  "O campo __example__ serve como dados de simulação para o modelo."
"""

from src.flows._crypto import _decrypt_request, _encrypt_response
from src.flows._token import decode_flow_token, encode_flow_token
from src.utils.log import logger

_TODAS: list[dict] = [
    {
        "id": "pagar_vista",
        "title": "Pagar à vista",
        "description": "Emitir guia para pagamento integral.",
    },
    {
        "id": "parcelar",
        "title": "Parcelar débitos",
        "description": "Simular e aderir ao parcelamento.",
    },
    {
        "id": "regularizar",
        "title": "Regularizar débitos",
        "description": "Ver alternativas para ficar em dia.",
    },
    {
        "id": "liquidar",
        "title": "Liquidar parcelamento",
        "description": "Quitar o que falta do parcelamento.",
    },
    {
        "id": "segunda_via",
        "title": "Emitir 2ª via",
        "description": "Gerar segunda via de guia/parcela.",
    },
    {
        "id": "voltar",
        "title": "Voltar",
        "description": "Retornar ao menu Tipos de consulta",
    },
]


def _build_opcoes(tem_nao_parcelado: bool, tem_parcelado: bool) -> list[dict]:
    if tem_nao_parcelado and tem_parcelado:
        ids = {
            "pagar_vista",
            "parcelar",
            "regularizar",
            "liquidar",
            "segunda_via",
            "voltar",
        }
    elif tem_nao_parcelado:
        ids = {"pagar_vista", "parcelar", "voltar"}
    else:
        ids = {"regularizar", "liquidar", "segunda_via", "voltar"}
    return [o for o in _TODAS if o["id"] in ids]


def build_opcoes_flow_token(
    base_token: str,
    total_nao_parcelado: int,
    total_parcelado: int,
) -> str:
    """Monta o flow_token com os flags de contexto para o flow de opções."""
    return encode_flow_token(
        base_token,
        {
            "tem_nao_parcelado": total_nao_parcelado > 0,
            "tem_parcelado": total_parcelado > 0,
        },
    )


def _handle_init(
    incoming_data: dict | None = None, flow_token: str | None = None
) -> dict:
    token_data = decode_flow_token(flow_token)
    tem_nao_parcelado = bool(token_data.get("tem_nao_parcelado", True))
    tem_parcelado = bool(token_data.get("tem_parcelado", True))

    opcoes = _build_opcoes(tem_nao_parcelado, tem_parcelado)
    logger.info(
        f"[OPCOES] INIT tem_nao_parcelado={tem_nao_parcelado} "
        f"tem_parcelado={tem_parcelado} → {[o['id'] for o in opcoes]}"
    )

    return {
        "version": "3.0",
        "screen": "OPCOES",
        "data": {"opcoes": opcoes},
    }


async def process_flow_request(body: dict, private_key_pem: str) -> str:
    try:
        payload, aes_key, iv = _decrypt_request(body, private_key_pem)
    except Exception as e:
        logger.error(f"opcoes_divida_ativa: erro ao decriptar: {e}")
        raise ValueError("Falha na decriptação do payload WhatsApp Flows") from e

    action = payload.get("action")
    flow_token = payload.get("flow_token")
    logger.info(f"opcoes_divida_ativa: action={action!r}")

    if action == "ping":
        response = {"data": {"status": "active"}}
    elif action == "INIT":
        response = _handle_init(payload.get("data", {}), flow_token=flow_token)
    else:
        logger.warning(f"opcoes_divida_ativa: action inesperada {action!r}")
        response = _handle_init(flow_token=flow_token)

    return _encrypt_response(response, aes_key, iv)
