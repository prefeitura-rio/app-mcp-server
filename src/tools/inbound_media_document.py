"""
Análise de documentos inbound do WhatsApp (Gemini) — PDF/TXT/CSV.

Cidadão envia um documento (PDF de conta, carta de reclamação, planilha) via
WhatsApp. A UM anexa como ContentVersion (validado ao vivo 2026-07-02:
FileType=PDF, attachment.pdf). Este analyzer baixa os bytes pela MESMA via
multi-source do image/audio (`resolve_inbound_bytes`: Meta CDN → Salesforce
ContentVersion → base64) e extrai o conteúdo relevante via Gemini 2.5 Flash,
que lê PDF e texto nativamente.

Formatos binários de Office (DOC/XLS/PPT) NÃO são lidos direto pelo Gemini —
ficam fora de `_ACCEPTED_EXTENSIONS` e o `resolve_inbound_bytes` já devolve o
fallback "descreva em texto". Registro canônico em `media-types.yaml`
(document.inbound).
"""

from typing import Any, Dict, Optional

from google import genai

from src.config import env
from src.utils.inbound_media_shared import (
    call_gemini_with_blob,
    deferred_no_bytes,
    deferred_no_gemini_key,
    error_gemini_failed,
    resolve_inbound_bytes,
)
from src.utils.log import logger

_MODEL = "gemini-2.5-flash"
_ACCEPTED_EXTENSIONS = {"pdf", "txt", "csv", "rtf"}
_MAX_BYTES = 20 * 1024 * 1024  # 20 MB — limite inline_data Gemini

# MIME do Graph API → extension canônica (o resolver normaliza via este map)
_MIME_TO_EXT = {
    "application/pdf": "pdf",
    "text/plain": "txt",
    "text/csv": "csv",
    "application/rtf": "rtf",
    "text/rtf": "rtf",
}

_EXT_TO_MIME = {
    "pdf": "application/pdf",
    "txt": "text/plain",
    "csv": "text/csv",
    "rtf": "application/rtf",
}

_ANALYSIS_PROMPT_PT_BR = """\
Você é um assistente da Prefeitura do Rio de Janeiro. Um cidadão enviou este
documento via WhatsApp durante um atendimento de serviço público.

Leia o documento e extraia, de forma objetiva e em português:
1. Um resumo do que o cidadão está pedindo ou reclamando (1-2 frases).
2. Endereço mencionado (rua, número, bairro), se houver.
3. Dados pessoais relevantes citados (nome, CPF, protocolo), se houver.
4. Qualquer prazo, data ou valor relevante.

Se o documento não tiver relação com serviço público, diga isso brevemente.
Responda em texto corrido curto, sem markdown. NÃO invente dados que não
estejam no documento.
"""


def _doc_mime_from_extension(file_extension: Optional[str]) -> str:
    """Extension → MIME pro Gemini blob. Default application/pdf (o tipo dominante)."""
    ext = (file_extension or "pdf").lower().lstrip(".")
    return _EXT_TO_MIME.get(ext, "application/pdf")


async def analyze_inbound_document(
    user_number: str,
    file_extension: Optional[str] = None,
    document_bytes_base64: Optional[str] = None,
    salesforce_download_path: Optional[str] = None,
    meta_media_id: Optional[str] = None,
    message_id: Optional[str] = None,
    content_version_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Baixa e extrai o conteúdo de um documento inbound (PDF/TXT/CSV) via Gemini.

    Fontes de bytes em ordem de preferência (via `resolve_inbound_bytes`):
    `meta_media_id` (Meta CDN) → `salesforce_download_path` (ContentVersion) →
    `document_bytes_base64` (testes). Retorna o conteúdo extraído + uma sugestão
    de resposta PT-BR; em qualquer falha, degrada com fallback "descreva em texto".
    """
    source = await resolve_inbound_bytes(
        tool_name="analyze_inbound_document",
        meta_media_id=meta_media_id,
        salesforce_download_path=salesforce_download_path,
        content_version_id=content_version_id,
        local_path=None,
        bytes_base64=document_bytes_base64,
        file_extension=file_extension,
        accepted_extensions=_ACCEPTED_EXTENSIONS,
        mime_to_extension=_MIME_TO_EXT,
        max_bytes=_MAX_BYTES,
        media_domain="document",
        local_path_reader=lambda _p: None,  # documento não vem de arquivo local
    )
    if source.error_response is not None:
        return source.error_response
    document_bytes = source.image_bytes  # campo genérico de bytes no resolver
    file_extension = source.file_extension

    if not document_bytes:
        return deferred_no_bytes("document")

    # Rejeita documento grande ANTES do Gemini (o resolver pode não barrar todas as
    # fontes; inline_data do Gemini tem teto). Evita erro/custo com PDF gigante.
    if len(document_bytes) > _MAX_BYTES:
        logger.warning(
            f"analyze_inbound_document: documento excede {_MAX_BYTES} bytes "
            f"({len(document_bytes)}) — rejeitado antes do Gemini "
            f"(content_version_id={content_version_id})"
        )
        return {
            "status": "rejected",
            "error": f"documento excede o limite de {_MAX_BYTES} bytes",
            "suggested_reply_pt_br": (
                "Seu documento é grande demais pra eu ler agora. Pode me contar em "
                "texto o que precisa, ou enviar um arquivo menor?"
            ),
        }

    if not env.GEMINI_API_KEY:
        logger.warning("analyze_inbound_document: GEMINI_API_KEY não configurada")
        return deferred_no_gemini_key()

    client = genai.Client(api_key=env.GEMINI_API_KEY)
    mime = _doc_mime_from_extension(file_extension)
    gemini_result = await call_gemini_with_blob(
        client=client,
        model=_MODEL,
        prompt_text=_ANALYSIS_PROMPT_PT_BR,
        mime_type=mime,
        blob_bytes=document_bytes,
        tool_name="analyze_inbound_document",
    )
    if gemini_result.text is None:
        return error_gemini_failed(
            gemini_result.error_detail or "Gemini call failed", "document"
        )

    extracted = gemini_result.text.strip()
    logger.info(
        f"analyze_inbound_document: user_number={user_number} "
        f"file_extension={file_extension!r} chars={len(extracted)} "
        f"message_id={message_id} content_version_id={content_version_id}"
    )
    return {
        "status": "analyzed",
        "analysis": {"conteudo_extraido": extracted},
        "suggested_reply_pt_br": (
            "Li o documento que você enviou e já considerei o conteúdo no seu "
            "atendimento."
            if extracted
            else "Recebi seu documento, mas não consegui ler o conteúdo agora. "
            "Pode me contar em texto o que você precisa?"
        ),
    }
