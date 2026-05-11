"""
Recepção de mídia inbound do WhatsApp (image/audio/location) via MCP.

Fluxo upstream (resumo):
    cidadão WhatsApp
      → BSP (Meta) → Salesforce UWC
      → Apex SCConnectFetchQueueable correlaciona ContentVersion e POSTs Mule
      → Mule /sc/inbound encaminha ao Gateway com {message_type, media: {...}}
      → Gateway encaminha ao Engine (Vertex AI Reasoning Engine)
      → Engine inclui o contexto da mídia no human message do LangGraph
      → Engine LLM chama esta tool MCP pra registrar a recepção

Comportamento atual: apenas REGISTRA (audit/log) + sugere resposta PT-BR.
Processamento real (visão pra imagens, transcrição pra áudios, geocoding pra
localização) ficará pra fase posterior — esta tool é o "receive-only stub"
que fecha o fluxo end-to-end até o MCP.

Ver `docs/onboarding.md` do `study-sf-whatsapp-poc1` (POC Salesforce side) e
ADR-012 (suporte a mídia inbound) pra contexto.
"""

from typing import Any, Dict, Optional

from src.utils.log import logger

_ACCEPTED_MEDIA_TYPES = {"image", "audio", "location", "unsupported", "unknown"}

# Respostas PT-BR sugeridas pelo agent IA quando a mídia recebida ainda não
# tem caminho de processamento ativo. O LLM pode adaptar mas o conteúdo aqui
# garante mensagem amigável + chamada-pra-ação (pedir texto).
_SUGGESTED_REPLIES_PT_BR = {
    "image": (
        "Recebi sua imagem! No momento ainda não consigo analisar fotos. "
        "Pode descrever em texto o que precisa pra eu te ajudar?"
    ),
    "audio": (
        "Recebi seu áudio! Estou aprendendo a entender mensagens de voz — "
        "por enquanto, pode escrever sua mensagem em texto?"
    ),
    "location": (
        "Recebi sua localização! Por enquanto preciso do endereço em texto "
        "(rua, número, bairro) pra prosseguir com a solicitação."
    ),
    "unsupported": (
        "Recebi sua mensagem, mas esse formato ainda não é suportado. "
        "Por favor, envie texto, imagem ou áudio."
    ),
    # 'unknown' = upstream (Apex) emite quando FileExtension nao casa whitelist
    # image/audio ou quando correlacao ContentVersion falha (quarantena).
    # Mesma mensagem amigavel de unsupported — diferenca é só telemetria.
    "unknown": (
        "Recebi sua mensagem, mas não consegui identificar o formato do anexo. "
        "Pode tentar enviar como texto, imagem ou áudio?"
    ),
}


async def register_inbound_media(
    media_type: str,
    user_number: str,
    message_id: Optional[str] = None,
    # Arquivo (image/audio) — vem de ContentVersion auto-attachado pelo bridge UWC:
    salesforce_download_path: Optional[str] = None,
    content_version_id: Optional[str] = None,
    file_extension: Optional[str] = None,
    file_size_bytes: Optional[int] = None,
    # Localização — não entregue pelo BSP atual; deixar pra suporte futuro:
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    address: Optional[str] = None,
    # Conversation correlation (auditoria + futura busca cruzada):
    messaging_session_id: Optional[str] = None,
    conversation_identifier: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Registra a recepção de uma mídia inbound do WhatsApp.

    Args:
        media_type: 'image' | 'audio' | 'location' | 'unsupported'.
        user_number: telefone E.164 sem prefixo `+` (ex: 5521989091014).
        message_id: UUID da entry Connect API (idempotency + cross-ref logs).
        salesforce_download_path: caminho REST relativo pro ContentVersion
            (`/services/data/v62.0/sobjects/ContentVersion/{Id}/VersionData`).
            Baixar requer session SF — não fazer aqui (defer).
        content_version_id: Id da ContentVersion auto-attachado pelo bridge UWC.
        file_extension: 'jpg'/'jpeg'/'png'/'gif'/'webp' (image) ou
            'oga'/'ogg'/'m4a'/'aac'/'amr'/'mp3' (audio).
        file_size_bytes: tamanho do arquivo.
        latitude/longitude/address: campos de localização (BSP atual NÃO entrega;
            placeholder pra suporte futuro).
        messaging_session_id: SF Id da MessagingSession (audit).
        conversation_identifier: UUID da Conversation Cloud (audit).

    Returns:
        Dict {status, media_type, processing, suggested_reply_pt_br, [error]}.
    """
    normalized = (media_type or "").strip().lower()
    if normalized not in _ACCEPTED_MEDIA_TYPES:
        logger.warning(
            f"register_inbound_media: media_type invalido '{media_type}' "
            f"de user_number={user_number}"
        )
        return {
            "status": "rejected",
            "error": f"media_type invalido: '{media_type}'",
            "accepted_types": sorted(_ACCEPTED_MEDIA_TYPES),
        }

    if not user_number or not str(user_number).strip():
        return {
            "status": "rejected",
            "error": "user_number eh obrigatorio",
        }

    audit_payload: Dict[str, Any] = {
        "media_type": normalized,
        "user_number": str(user_number).strip(),
        "message_id": message_id,
        "salesforce_download_path": salesforce_download_path,
        "content_version_id": content_version_id,
        "file_extension": file_extension,
        "file_size_bytes": file_size_bytes,
        "latitude": latitude,
        "longitude": longitude,
        "address": address,
        "messaging_session_id": messaging_session_id,
        "conversation_identifier": conversation_identifier,
    }
    # Log limpo: omite chaves None pra reduzir ruído.
    log_payload = {k: v for k, v in audit_payload.items() if v is not None}
    logger.info(f"register_inbound_media (receive-stub): {log_payload}")

    return {
        "status": "received",
        "media_type": normalized,
        "processing": "deferred",
        "suggested_reply_pt_br": _SUGGESTED_REPLIES_PT_BR[normalized],
    }
