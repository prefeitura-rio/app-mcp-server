"""
Detecção de tipo de mídia por magic bytes (file header).

Motivação: o Gemini multimodal (Vision + audio) NÃO valida se os bytes
recebidos via ``inline_data`` batem com o ``mime_type`` declarado. Quando
recebe bytes de imagem com ``mime_type=audio/ogg`` (ou vice-versa, ou
WAV bytes anunciados como audio/ogg, etc.), o modelo às vezes retorna
análise vazia/baixa-confiança e às vezes **alucina** uma resposta
plausível (transcrição inventada de denúncia detalhada, descrição
visual fictícia, etc.). Esse comportamento foi observado em smoke test
2026-05-14: ContentVersion `0688800000Bgd3T` (JPG de propaganda)
enviado como audio gerou uma vez transcrição de denúncia de luminária
inventada, outra vez retornou vazio.

Defesa: antes de chamar o Gemini, comparar magic bytes com a extension
declarada de forma **granular**. Se o subtype detectado for diferente
do subtype derivado da extension, rejeitar a chamada com erro claro —
protege contra:

1. **Apex misclassification**: race entre múltiplas mídias na mesma
   sessão pode correlacionar ContentVersion errado.
2. **Subtype mismatch**: bytes WAV anunciados como `oga` resultariam
   em `mime_type=audio/ogg` enviado ao Gemini — o modelo provavelmente
   falha ou aluciona porque o container declarado não casa.
3. **Prompt injection**: LLM com instrução adversarial poderia tentar
   alterar a extension declarada. Magic bytes do arquivo real pegam.
4. **Corrupção de dados**: ContentVersion com bytes corrompidos não
   passa pra Gemini.

Os magic bytes verificados são prefixos de **container/codec headers**,
não tags semânticas — não há falso positivo razoável.
"""

from typing import Optional


def _starts_with(data: bytes, prefix: bytes, offset: int = 0) -> bool:
    """Helper: confere se `data` em `offset` tem `prefix`."""
    return (
        len(data) >= offset + len(prefix)
        and data[offset : offset + len(prefix)] == prefix
    )


# Mapeia ``file_extension`` (case-insensitive, sem ponto) pro subtype
# canônico retornado por :func:`detect_media_subtype`. Mantemos só os
# formatos do allowlist do Gemini Vision/audio input + os que o WhatsApp
# costuma entregar.
_EXTENSION_TO_SUBTYPE = {
    # imagens
    "jpg": "jpeg",
    "jpeg": "jpeg",
    "png": "png",
    "gif": "gif",
    "webp": "webp",
    # áudios
    "oga": "ogg",
    "ogg": "ogg",
    "mp3": "mp3",
    "aac": "aac",
    "wav": "wav",
    "flac": "flac",
    "aiff": "aiff",
    "aif": "aiff",
}


def detect_media_subtype(data: bytes) -> Optional[str]:
    """
    Retorna o subtype canônico do arquivo baseado em magic bytes, ou
    ``None`` se não bate nenhum tipo conhecido.

    Subtypes retornados (mesmo formato usado em :data:`_EXTENSION_TO_SUBTYPE`):

    * Imagens: ``"jpeg"``, ``"png"``, ``"gif"``, ``"webp"``
    * Áudios: ``"ogg"``, ``"mp3"``, ``"aac"``, ``"wav"``, ``"flac"``, ``"aiff"``

    A função é proposital e estritamente defensiva: só reconhece formatos
    que o restante do código aceita. Qualquer outro tipo (PDF, ZIP, vídeo
    container, etc.) volta como ``None``.
    """
    if not data or len(data) < 4:
        return None

    # --- Image headers ---
    # JPEG: FF D8 (SOI). Demais bytes são marker dependente do encoder.
    if _starts_with(data, b"\xff\xd8"):
        return "jpeg"
    # PNG: 89 50 4E 47 0D 0A 1A 0A
    if _starts_with(data, b"\x89PNG\r\n\x1a\n"):
        return "png"
    # GIF: GIF87a ou GIF89a
    if _starts_with(data, b"GIF87a") or _starts_with(data, b"GIF89a"):
        return "gif"
    # WEBP: "RIFF....WEBP"
    if _starts_with(data, b"RIFF") and _starts_with(data, b"WEBP", offset=8):
        return "webp"

    # --- Audio headers ---
    # OGG container (Opus, Vorbis): "OggS"
    if _starts_with(data, b"OggS"):
        return "ogg"
    # MP3: ID3v2 header "ID3" ou frame sync 0xFFE0-FFFF top 11 bits.
    if _starts_with(data, b"ID3"):
        return "mp3"
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        # MPEG audio frame sync (top 11 bits == 0b11111111111). Tem que
        # discriminar MP3 (layer II/III) vs AAC ADTS (layer 00).
        # Layout do byte[1] (8 bits): SSSS VVLL CC onde
        #   SSSS = sync trailing (sempre 1111)
        #   VV   = MPEG version (00=2.5, 01=reserved, 10=2, 11=1)
        #   LL   = layer (00=AAC, 01=layer III, 10=layer II, 11=layer I)
        #   CC   = protection bit (sempre 1=ausente ou 0=CRC presente)
        # AAC ADTS: layer == 00 → byte[1] & 0x06 == 0
        # MP3:      layer != 00 → byte[1] & 0x06 != 0
        # ADTS válido: byte[1] in {0xF0, 0xF1, 0xF8, 0xF9} (MPEG 2 ou 4,
        # com/sem CRC). MP3 válido tipicamente 0xFA-0xFB ou 0xF2-0xF3.
        if (data[1] & 0x06) == 0:
            return "aac"
        return "mp3"
    # AAC ADIF: "ADIF" (raro mas valido pra .aac sem container ADTS).
    if _starts_with(data, b"ADIF"):
        return "aac"
    # WAV: "RIFF....WAVE"
    if _starts_with(data, b"RIFF") and _starts_with(data, b"WAVE", offset=8):
        return "wav"
    # FLAC: "fLaC"
    if _starts_with(data, b"fLaC"):
        return "flac"
    # AIFF: "FORM....AIFF" (ou AIFC pra AIFF-C)
    if _starts_with(data, b"FORM") and (
        _starts_with(data, b"AIFF", offset=8) or _starts_with(data, b"AIFC", offset=8)
    ):
        return "aiff"

    return None


def detect_media_kind(data: bytes) -> Optional[str]:
    """
    Retorna ``'image'`` ou ``'audio'`` baseado nos primeiros bytes, ou
    ``None`` se não bate nenhum tipo conhecido. Wrapper de
    :func:`detect_media_subtype` mantido para retrocompatibilidade.
    """
    subtype = detect_media_subtype(data)
    if subtype is None:
        return None
    if subtype in {"jpeg", "png", "gif", "webp"}:
        return "image"
    if subtype in {"ogg", "mp3", "aac", "wav", "flac", "aiff"}:
        return "audio"
    return None


def matches_expected_kind(data: bytes, expected: str) -> bool:
    """
    Retorna True se os magic bytes do arquivo batem com `expected`
    (``'image'`` ou ``'audio'``) — comparação grossa, suficiente para
    cross-media (ex: JPG declarado como audio).
    """
    return detect_media_kind(data) == expected


def matches_expected_extension(data: bytes, file_extension: Optional[str]) -> bool:
    """
    Retorna True se os magic bytes do arquivo batem com a extension
    declarada — comparação **subtype-granular**. Pega tanto cross-media
    quanto mismatches dentro do mesmo kind:

    * JPG declarado como ``oga`` → False (cross-media)
    * WAV declarado como ``oga`` → False (subtype diferente; o
      ``mime_type`` derivado da extension seria ``audio/ogg`` mas os
      bytes são ``audio/wav``, levando o Gemini a alucinar ou falhar)
    * PNG declarado como ``jpg`` → False (mesma razão)
    * OGG declarado como ``oga`` ou ``ogg`` → True (subtype canônico)

    Se a extensão não está em :data:`_EXTENSION_TO_SUBTYPE` (formato não
    aceito), retorna False — caller já deveria ter rejeitado em outro
    ponto, mas o gate é defensivo.
    """
    if not file_extension:
        return False
    ext = file_extension.lower().lstrip(".")
    expected_subtype = _EXTENSION_TO_SUBTYPE.get(ext)
    if expected_subtype is None:
        return False
    return detect_media_subtype(data) == expected_subtype
