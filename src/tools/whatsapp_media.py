"""
WhatsApp outbound media — tool passthrough que constrói o envelope
canônico consumido pelo Mule (`vars.agentMedia` em webhook-flow.xml).
ADR-022.

Permite ao LLM responder com qualquer tipo de mídia outbound sem que
o MCP precise carregar a mídia em si — Mule faz o upload (Meta /media)
ou usa link direto.

Pra image/video/document/sticker/audio: passar `url` (link público que
o Meta busca) OU `base64` (Mule decoda + upload via /media).

Pra location: latitude + longitude obrigatórios; name + address opcionais.

Pra contacts/interactive: passthrough do schema Meta Business API.

Esta função vive separada do `app.py` pra ser unit-testable.
"""

from __future__ import annotations

from typing import Any, Optional

_VALID_TYPES = {
    "audio",
    "image",
    "video",
    "document",
    "sticker",
    "location",
    "contacts",
    "interactive",
    "template",
    "reaction",
}

_UPLOAD_TYPES = {"audio", "image", "video", "document", "sticker"}


def build_whatsapp_media_envelope(
    type: str,
    url: Optional[str] = None,
    base64: Optional[str] = None,
    mime_type: Optional[str] = None,
    caption: Optional[str] = None,
    filename: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    name: Optional[str] = None,
    address: Optional[str] = None,
    contacts: Optional[list[Any]] = None,
    interactive: Optional[dict[str, Any]] = None,
    template: Optional[dict[str, Any]] = None,
    reaction_to_message_id: Optional[str] = None,
    emoji: Optional[str] = None,
) -> dict[str, Any]:
    """Valida os args e retorna envelope canônico que Mule consome.

    Retorna `{status: "ok", type, ...campos populados}` em sucesso.
    Retorna `{status: "error", error: <msg>}` em validação falha (o Mule
    detecta status != "ok" e cai pra texto).
    """
    if type not in _VALID_TYPES:
        return {
            "status": "error",
            "error": f"type inválido: '{type}'. Permitidos: {sorted(_VALID_TYPES)}",
        }
    if type in _UPLOAD_TYPES:
        if not (url or base64):
            return {
                "status": "error",
                "error": f"type={type} requer `url` ou `base64`",
            }
        if url and base64:
            return {
                "status": "error",
                "error": (
                    f"type={type} aceita APENAS `url` OU `base64` (mutuamente "
                    "exclusivos). Mule não consegue dispatchar link + upload "
                    "no mesmo envelope."
                ),
            }
        if url and not url.startswith("https://"):
            # Meta Graph API requer URLs HTTPS pra media. http:// é rejeitado
            # com erro 100 (parameter invalid). Bloquear no tool-call evita
            # surprise downstream no Mule.
            return {
                "status": "error",
                "error": (
                    f"type={type} com `url` requer protocolo HTTPS. "
                    f"Recebido: '{url[:60]}{'…' if len(url) > 60 else ''}'. "
                    "Meta Graph API rejeita HTTP."
                ),
            }
        if base64 and not mime_type:
            # Mule meta-upload-media-sub-flow tem defaults por tipo, mas guess
            # errado quebra o upload Meta (mime declarado != content). Codex
            # P2: melhor falhar no tool-call que adivinhar.
            return {
                "status": "error",
                "error": (
                    f"type={type} com `base64` requer `mime_type` explícito "
                    "(ex: image/jpeg, image/png, video/mp4, audio/ogg, "
                    "application/pdf, image/webp). Sem ele o Meta /media "
                    "rejeita o upload por mime/content mismatch."
                ),
            }
    if type == "location" and (latitude is None or longitude is None):
        return {
            "status": "error",
            "error": "type=location requer `latitude` e `longitude`",
        }
    if type == "contacts" and not contacts:
        return {
            "status": "error",
            "error": "type=contacts requer `contacts` list não-vazia",
        }
    if type == "interactive" and not interactive:
        return {
            "status": "error",
            "error": "type=interactive requer `interactive` object não-vazio",
        }
    if type == "template" and (not template or not template.get("name")):
        return {
            "status": "error",
            "error": (
                "type=template requer `template` object com pelo menos "
                "`name` (do template aprovado no Meta Business Manager) e "
                "`language` (e.g. {code: 'pt_BR'})."
            ),
        }
    if type == "reaction":
        if not reaction_to_message_id:
            return {
                "status": "error",
                "error": (
                    "type=reaction requer `reaction_to_message_id` "
                    "(wamid da mensagem inbound do cidadão que será reagida)."
                ),
            }
        if not emoji:
            return {
                "status": "error",
                "error": (
                    "type=reaction requer `emoji` (string Unicode com 1 "
                    "emoji, e.g. '👍' '❤️'). Vazio = remove reação existente."
                ),
            }

    envelope: dict[str, Any] = {"status": "ok", "type": type}
    if base64:
        envelope["base64"] = base64
    if url:
        envelope["url"] = url
    if mime_type:
        envelope["mime_type"] = mime_type
    if caption:
        envelope["caption"] = caption
    if filename:
        envelope["filename"] = filename
    if latitude is not None:
        envelope["latitude"] = latitude
    if longitude is not None:
        envelope["longitude"] = longitude
    if name:
        envelope["name"] = name
    if address:
        envelope["address"] = address
    if contacts is not None:
        envelope["contacts"] = contacts
    if interactive is not None:
        envelope["interactive"] = interactive
    if template is not None:
        envelope["template"] = template
    if reaction_to_message_id is not None:
        envelope["reaction_to_message_id"] = reaction_to_message_id
    if emoji is not None:
        envelope["emoji"] = emoji
    return envelope
