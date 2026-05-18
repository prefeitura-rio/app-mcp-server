"""Unit tests pro build_whatsapp_media_envelope (ADR-022)."""

from src.tools.whatsapp_media import build_whatsapp_media_envelope


def test_audio_inline_base64_ok():
    env = build_whatsapp_media_envelope(
        type="audio", base64="T2dnUw...", mime_type="audio/ogg"
    )
    assert env["status"] == "ok"
    assert env["type"] == "audio"
    assert env["base64"] == "T2dnUw..."
    assert env["mime_type"] == "audio/ogg"
    assert "url" not in env


def test_image_url_with_caption():
    env = build_whatsapp_media_envelope(
        type="image",
        url="https://example.com/img.jpg",
        caption="Foto da rua",
    )
    assert env["status"] == "ok"
    assert env["type"] == "image"
    assert env["url"] == "https://example.com/img.jpg"
    assert env["caption"] == "Foto da rua"


def test_document_with_filename():
    env = build_whatsapp_media_envelope(
        type="document",
        url="https://example.com/protocol.pdf",
        filename="protocolo-1234.pdf",
        caption="Comprovante",
    )
    assert env["status"] == "ok"
    assert env["filename"] == "protocolo-1234.pdf"
    assert env["caption"] == "Comprovante"


def test_location_complete():
    env = build_whatsapp_media_envelope(
        type="location",
        latitude=-22.9068,
        longitude=-43.1729,
        name="Cristo Redentor",
        address="Parque da Tijuca",
    )
    assert env["status"] == "ok"
    assert env["latitude"] == -22.9068
    assert env["longitude"] == -43.1729
    assert env["name"] == "Cristo Redentor"


def test_location_zero_coordinates_still_valid():
    # latitude=0 / longitude=0 são valores válidos (null island na pratica
    # mas legais). Não devem ser tratados como missing.
    env = build_whatsapp_media_envelope(type="location", latitude=0.0, longitude=0.0)
    assert env["status"] == "ok"
    assert env["latitude"] == 0.0


def test_location_missing_coords_returns_error():
    env = build_whatsapp_media_envelope(type="location", latitude=-22.9)
    assert env["status"] == "error"
    assert "latitude" in env["error"] and "longitude" in env["error"]


def test_upload_type_without_url_or_base64_returns_error():
    env = build_whatsapp_media_envelope(type="image")
    assert env["status"] == "error"
    assert "url" in env["error"] and "base64" in env["error"]


def test_invalid_type_returns_error():
    env = build_whatsapp_media_envelope(type="hologram", url="https://x.y")
    assert env["status"] == "error"
    assert "type inválido" in env["error"]


def test_contacts_passthrough():
    env = build_whatsapp_media_envelope(
        type="contacts",
        contacts=[
            {"name": {"formatted_name": "Maria"}, "phones": [{"phone": "+5521..."}]}
        ],
    )
    assert env["status"] == "ok"
    assert env["type"] == "contacts"
    assert len(env["contacts"]) == 1


def test_interactive_passthrough():
    env = build_whatsapp_media_envelope(
        type="interactive",
        interactive={
            "type": "flow",
            "header": {"type": "text", "text": "Reportar luminária"},
            "body": {"text": "Preencha o formulário"},
            "action": {"name": "flow", "parameters": {"flow_id": "4141008006029185"}},
        },
    )
    assert env["status"] == "ok"
    assert env["type"] == "interactive"
    assert env["interactive"]["type"] == "flow"


def test_video_with_url_only():
    env = build_whatsapp_media_envelope(type="video", url="https://example.com/v.mp4")
    assert env["status"] == "ok"
    assert env["type"] == "video"
    assert env["url"] == "https://example.com/v.mp4"
    # Sem caption/filename setados, não devem aparecer
    assert "caption" not in env
    assert "filename" not in env


def test_sticker_inline():
    env = build_whatsapp_media_envelope(
        type="sticker", base64="UklGR...", mime_type="image/webp"
    )
    assert env["status"] == "ok"
    assert env["type"] == "sticker"


def test_base64_without_mime_type_returns_error():
    env = build_whatsapp_media_envelope(type="image", base64="iVBORw0K...")
    assert env["status"] == "error"
    assert "mime_type" in env["error"]


def test_http_url_returns_error():
    env = build_whatsapp_media_envelope(
        type="image", url="http://insecure.example/img.jpg"
    )
    assert env["status"] == "error"
    assert "HTTPS" in env["error"]


def test_https_url_accepted():
    env = build_whatsapp_media_envelope(
        type="image", url="https://secure.example/img.jpg"
    )
    assert env["status"] == "ok"
    assert env["url"] == "https://secure.example/img.jpg"


def test_url_and_base64_simultaneously_returns_error():
    env = build_whatsapp_media_envelope(
        type="image", url="https://x.y/img.jpg", base64="iVBORw0K..."
    )
    assert env["status"] == "error"
    assert "mutuamente exclusivos" in env["error"]


def test_contacts_empty_list_returns_error():
    env = build_whatsapp_media_envelope(type="contacts", contacts=[])
    assert env["status"] == "error"


def test_contacts_omitted_returns_error():
    env = build_whatsapp_media_envelope(type="contacts")
    assert env["status"] == "error"


def test_interactive_empty_dict_returns_error():
    env = build_whatsapp_media_envelope(type="interactive", interactive={})
    assert env["status"] == "error"


def test_interactive_omitted_returns_error():
    env = build_whatsapp_media_envelope(type="interactive")
    assert env["status"] == "error"


def test_empty_optionals_not_included():
    env = build_whatsapp_media_envelope(
        type="audio",
        base64="data",
        mime_type="audio/ogg",
        caption="",
        filename="",
    )
    # caption="" e filename="" são falsy, devem ser omitidos
    assert env["status"] == "ok"
    assert "caption" not in env
    assert "filename" not in env
