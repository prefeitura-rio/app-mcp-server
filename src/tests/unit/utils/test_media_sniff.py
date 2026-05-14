"""
Testes de src/utils/media_sniff.py — detecção de tipo de mídia por magic
bytes. Garante que arquivos com extensão declarada errada (ou bytes
corrompidos) são rejeitados antes de chegar no Gemini, evitando
alucinações.
"""

from src.utils.media_sniff import (
    detect_media_kind,
    detect_media_subtype,
    matches_expected_extension,
    matches_expected_kind,
)


# ---------- detect_media_subtype: subtypes granulares ----------


def test_detect_subtype_jpeg():
    assert detect_media_subtype(b"\xff\xd8\xff\xe0" + b"x" * 100) == "jpeg"


def test_detect_subtype_png():
    assert detect_media_subtype(b"\x89PNG\r\n\x1a\n" + b"x" * 100) == "png"


def test_detect_subtype_gif():
    assert detect_media_subtype(b"GIF89a" + b"x" * 100) == "gif"


def test_detect_subtype_webp():
    assert detect_media_subtype(b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"x" * 100) == "webp"


def test_detect_subtype_ogg():
    assert detect_media_subtype(b"OggS" + b"\x00" * 100) == "ogg"


def test_detect_subtype_mp3_id3():
    assert detect_media_subtype(b"ID3\x03\x00\x00\x00" + b"x" * 100) == "mp3"


def test_detect_subtype_aac_adts_mpeg4_no_crc():
    # ADTS MPEG-4, sem CRC (byte 1 = 0xF1 → layer bits = 00)
    assert detect_media_subtype(b"\xff\xf1\x50\x80" + b"x" * 100) == "aac"


def test_detect_subtype_aac_adts_mpeg4_with_crc():
    # ADTS MPEG-4, COM CRC (byte 1 = 0xF0 → layer bits = 00 + protection=0)
    assert detect_media_subtype(b"\xff\xf0\x50\x80" + b"x" * 100) == "aac"


def test_detect_subtype_aac_adts_mpeg2_no_crc():
    # ADTS MPEG-2, sem CRC (byte 1 = 0xF9)
    assert detect_media_subtype(b"\xff\xf9\x50\x80" + b"x" * 100) == "aac"


def test_detect_subtype_aac_adts_mpeg2_with_crc():
    # ADTS MPEG-2, COM CRC (byte 1 = 0xF8)
    assert detect_media_subtype(b"\xff\xf8\x50\x80" + b"x" * 100) == "aac"


def test_detect_subtype_mp3_mpeg1_layer3():
    # MP3 MPEG-1 Layer III sem CRC (byte 1 = 0xFB)
    assert detect_media_subtype(b"\xff\xfb\x90\x00" + b"x" * 100) == "mp3"


def test_detect_subtype_mp3_mpeg2_layer3():
    # MP3 MPEG-2 Layer III sem CRC (byte 1 = 0xF3)
    assert detect_media_subtype(b"\xff\xf3\x90\x00" + b"x" * 100) == "mp3"


def test_detect_subtype_aac_adif():
    # ADIF AAC stream (raro mas válido para .aac)
    assert detect_media_subtype(b"ADIF\x00\x00" + b"x" * 100) == "aac"


def test_detect_subtype_wav():
    assert detect_media_subtype(b"RIFF\x00\x00\x00\x00WAVEfmt " + b"x" * 100) == "wav"


def test_detect_subtype_flac():
    assert detect_media_subtype(b"fLaC\x00" + b"x" * 100) == "flac"


def test_detect_subtype_aiff():
    assert detect_media_subtype(b"FORM\x00\x00\x00\x00AIFFCOMM" + b"x" * 100) == "aiff"


def test_detect_subtype_pdf_returns_none():
    assert detect_media_subtype(b"%PDF-1.7\n" + b"x" * 100) is None


# ---------- matches_expected_extension: subtype-granular ----------


def test_extension_oga_matches_ogg_bytes():
    assert matches_expected_extension(b"OggS" + b"x" * 100, "oga") is True


def test_extension_ogg_matches_ogg_bytes():
    assert matches_expected_extension(b"OggS" + b"x" * 100, "ogg") is True


def test_extension_jpg_matches_jpeg_bytes():
    assert matches_expected_extension(b"\xff\xd8\xff\xe0" + b"x" * 100, "jpg") is True


def test_extension_jpeg_matches_jpeg_bytes():
    assert matches_expected_extension(b"\xff\xd8\xff\xe0" + b"x" * 100, "jpeg") is True


def test_wav_bytes_do_not_match_oga_extension():
    """Subtype mismatch: WAV bytes declarados como `.oga` ⇒ False.
    Sem isso, mime_type=audio/ogg seria enviado pro Gemini com bytes WAV."""
    wav_bytes = b"RIFF\x00\x00\x00\x00WAVEfmt " + b"x" * 100
    assert matches_expected_extension(wav_bytes, "oga") is False


def test_png_bytes_do_not_match_jpg_extension():
    """PNG declarado como JPG ⇒ False. mime_type=image/jpeg seria mandado
    pro Gemini com bytes PNG."""
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"x" * 100
    assert matches_expected_extension(png_bytes, "jpg") is False


def test_jpeg_does_not_match_audio_extension():
    """Cross-media: JPG declarado como audio."""
    jpg_bytes = b"\xff\xd8\xff\xe0" + b"x" * 100
    assert matches_expected_extension(jpg_bytes, "oga") is False


def test_unknown_extension_returns_false():
    """Extension fora do allowlist defensivo retorna False."""
    assert matches_expected_extension(b"\xff\xd8\xff\xe0" + b"x" * 100, "tiff") is False


def test_aac_adif_matches_aac_extension():
    """ADIF AAC era rejeitado na 1a versão; tem que matchar agora."""
    assert matches_expected_extension(b"ADIF\x00\x00" + b"x" * 100, "aac") is True


def test_empty_extension_returns_false():
    assert matches_expected_extension(b"OggS" + b"x" * 100, None) is False
    assert matches_expected_extension(b"OggS" + b"x" * 100, "") is False


# ---------- detect_media_kind: imagens ----------


def test_detects_jpeg_by_ff_d8():
    # JPEG SOI marker
    assert detect_media_kind(b"\xff\xd8\xff\xe0" + b"x" * 100) == "image"


def test_detects_png_by_signature():
    assert detect_media_kind(b"\x89PNG\r\n\x1a\n" + b"x" * 100) == "image"


def test_detects_gif_87a():
    assert detect_media_kind(b"GIF87a" + b"x" * 100) == "image"


def test_detects_gif_89a():
    assert detect_media_kind(b"GIF89a" + b"x" * 100) == "image"


def test_detects_webp_riff_webp():
    # "RIFF" + 4 bytes size + "WEBP"
    assert detect_media_kind(b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"x" * 100) == "image"


# ---------- detect_media_kind: áudios ----------


def test_detects_ogg_by_ogg_s():
    # WhatsApp PTT chega como OGG-Opus
    assert detect_media_kind(b"OggS" + b"\x00" * 100) == "audio"


def test_detects_mp3_by_id3_tag():
    assert detect_media_kind(b"ID3\x03\x00\x00\x00" + b"x" * 100) == "audio"


def test_detects_mp3_by_frame_sync():
    # MPEG audio frame sync: 0xFF 0xE0..0xFF (top 11 bits = 1)
    assert detect_media_kind(b"\xff\xfb\x90\x00" + b"x" * 100) == "audio"


def test_detects_wav_riff_wave():
    assert detect_media_kind(b"RIFF\x00\x00\x00\x00WAVEfmt " + b"x" * 100) == "audio"


def test_detects_flac_by_signature():
    assert detect_media_kind(b"fLaC\x00\x00\x00\x22" + b"x" * 100) == "audio"


def test_detects_aiff_form_aiff():
    assert detect_media_kind(b"FORM\x00\x00\x00\x00AIFFCOMM" + b"x" * 100) == "audio"


def test_detects_aifc_form_aifc():
    # AIFF-C variant
    assert detect_media_kind(b"FORM\x00\x00\x00\x00AIFCFVER" + b"x" * 100) == "audio"


# ---------- detect_media_kind: rejeições ----------


def test_returns_none_for_pdf():
    # PDF não está no escopo do bot
    assert detect_media_kind(b"%PDF-1.7\n" + b"x" * 100) is None


def test_returns_none_for_zip():
    assert detect_media_kind(b"PK\x03\x04" + b"x" * 100) is None


def test_returns_none_for_random_text():
    assert detect_media_kind(b"This is some random text without magic bytes.") is None


def test_returns_none_for_empty_bytes():
    assert detect_media_kind(b"") is None


def test_returns_none_for_short_input():
    # Menos de 4 bytes → não dá pra decidir
    assert detect_media_kind(b"AB") is None


def test_returns_none_when_riff_but_no_webp_or_wave():
    # "RIFF" só sem WEBP/WAVE = container desconhecido (talvez AVI/etc).
    # Conservativo: rejeita.
    assert detect_media_kind(b"RIFF\x00\x00\x00\x00AVI LIST" + b"x" * 100) is None


def test_returns_none_when_form_but_no_aiff():
    assert detect_media_kind(b"FORM\x00\x00\x00\x008SVX" + b"x" * 100) is None


# ---------- matches_expected_kind: lógica de comparação ----------


def test_matches_ogg_as_audio():
    assert matches_expected_kind(b"OggS" + b"x" * 100, "audio") is True


def test_jpeg_does_not_match_audio():
    # O bug que motivou esta defesa: JPG declarado como audio
    assert matches_expected_kind(b"\xff\xd8\xff\xe0" + b"x" * 100, "audio") is False


def test_ogg_does_not_match_image():
    # E o reverso
    assert matches_expected_kind(b"OggS" + b"x" * 100, "image") is False


def test_matches_jpeg_as_image():
    assert matches_expected_kind(b"\xff\xd8\xff\xe0" + b"x" * 100, "image") is True


def test_unknown_does_not_match_anything():
    pdf_like = b"%PDF-1.7\n" + b"x" * 100
    assert matches_expected_kind(pdf_like, "image") is False
    assert matches_expected_kind(pdf_like, "audio") is False


def test_empty_bytes_does_not_match():
    assert matches_expected_kind(b"", "audio") is False
    assert matches_expected_kind(b"", "image") is False
