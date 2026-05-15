"""Tests do TTS (generate_audio_response)."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from src.tools.tts import AudioResponseResult, generate_audio_response


def test_audio_response_result_to_dict_minimal():
    r = AudioResponseResult(status="error", error="x")
    out = r.to_dict()
    assert out["status"] == "error"
    assert out["error"] == "x"
    assert "audio_base64" not in out


def test_audio_response_result_to_dict_ok():
    r = AudioResponseResult(
        status="ok",
        audio_base64="QUJD",
        duration_estimate_s=3.5,
        voice_used="pt-BR-Neural2-A",
    )
    out = r.to_dict()
    assert out["status"] == "ok"
    assert out["audio_base64"] == "QUJD"
    assert out["duration_estimate_s"] == 3.5
    assert out["voice_used"] == "pt-BR-Neural2-A"


def test_empty_text_rejected():
    result = asyncio.run(generate_audio_response(text=""))
    assert result["status"] == "error"
    assert "vazio" in result["error"]


def test_whitespace_only_rejected():
    result = asyncio.run(generate_audio_response(text="   \n\t  "))
    assert result["status"] == "error"


def test_too_long_text_rejected():
    long_text = "a" * 5001
    result = asyncio.run(generate_audio_response(text=long_text))
    assert result["status"] == "error"
    assert "5000" in result["error"]


def test_client_construction_failure_returns_error():
    """Quando TextToSpeechAsyncClient() falha (ex: auth error), tool captura
    a exception e retorna status='error' com detalhe. Mockamos a classe pra
    raise DefaultCredentialsError — não chama API real, não consome quota,
    não depende de ambiente.
    """
    import importlib

    fake_module = type("M", (), {})()

    class FakeCredsError(Exception):
        pass

    def fake_client_ctor(*_a, **_kw):
        raise FakeCredsError("no credentials available")

    fake_module.TextToSpeechAsyncClient = fake_client_ctor
    fake_module.SynthesisInput = lambda **_kw: None
    fake_module.VoiceSelectionParams = lambda **_kw: None
    fake_module.AudioConfig = lambda **_kw: None

    class FakeAudioEncoding:
        OGG_OPUS = 1

    fake_module.AudioEncoding = FakeAudioEncoding

    fake_pkg = type("P", (), {"texttospeech": fake_module})()
    with patch.dict("sys.modules", {"google.cloud": fake_pkg}):
        importlib.invalidate_caches()
        result = asyncio.run(generate_audio_response(text="Olá"))
        assert result["status"] == "error"
        assert (
            "FakeCredsError" in result["error"] or "no credentials" in result["error"]
        )
