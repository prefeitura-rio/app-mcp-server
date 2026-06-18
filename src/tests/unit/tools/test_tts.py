"""Tests do TTS (generate_audio_response)."""

from __future__ import annotations

import asyncio
import base64
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import src.tools.tts as tts_module
from src.tools.tts import AudioResponseResult, generate_audio_response


@pytest.fixture(autouse=True)
def _tts_cache_off_by_default(monkeypatch):
    """Cache do TTS OFF por default nos testes: evita que o Redis disponível no CI
    faça o cache CRUZAR resultados entre testes que compartilham o mesmo texto
    (ex.: "Olá cidadão"). Os testes de cache religam explicitamente via
    patch.dict(os.environ, {"TTS_CACHE_ENABLED": "true"})."""
    monkeypatch.setenv("TTS_CACHE_ENABLED", "false")


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
    with (
        patch("src.tools.tts.env.TTS_PROVIDER", "google"),
        patch.dict("sys.modules", {"google.cloud": fake_pkg}),
    ):
        importlib.invalidate_caches()
        result = asyncio.run(generate_audio_response(text="Olá"))
        assert result["status"] == "error"
        assert (
            "FakeCredsError" in result["error"] or "no credentials" in result["error"]
        )


# --- Provider dispatch (TTS_PROVIDER switch, ver ADR-038) -------------------


def test_dispatch_defaults_to_google():
    async def fake_google(_cleaned):
        return b"OGGGOOGLE", "pt-BR-Neural2-A"

    async def fake_gemini(_cleaned):
        raise AssertionError("gemini não deveria rodar quando provider=google")

    with (
        patch("src.tools.tts.env.TTS_PROVIDER", "google"),
        patch("src.tools.tts._synthesize_google", fake_google),
        patch("src.tools.tts._synthesize_gemini", fake_gemini),
    ):
        result = asyncio.run(generate_audio_response(text="Olá cidadão"))
    assert result["status"] == "ok"
    assert result["voice_used"] == "pt-BR-Neural2-A"
    assert result["mime_type"] == "audio/ogg"
    assert result["audio_base64"] == base64.b64encode(b"OGGGOOGLE").decode("ascii")


def test_dispatch_gemini_when_provider_set():
    async def fake_google(_cleaned):
        raise AssertionError("google não deveria rodar quando provider=gemini")

    async def fake_gemini(_cleaned):
        return b"OGGGEMINI", "Sulafat"

    with (
        patch("src.tools.tts.env.TTS_PROVIDER", "gemini"),
        patch("src.tools.tts._synthesize_google", fake_google),
        patch("src.tools.tts._synthesize_gemini", fake_gemini),
    ):
        result = asyncio.run(generate_audio_response(text="Olá cidadão"))
    assert result["status"] == "ok"
    assert result["voice_used"] == "Sulafat"
    assert result["audio_base64"] == base64.b64encode(b"OGGGEMINI").decode("ascii")


# --- Fallback gemini→google (resiliência de áudio, POC1 #303) ---------------


def test_gemini_failure_falls_back_to_google():
    """provider=gemini mas o Gemini falha → entrega áudio via Google (a voz
    muda, mas o cidadão que não lê recebe áudio em vez de nada)."""

    async def fake_google(_cleaned):
        return b"OGGGOOGLE", "pt-BR-Neural2-A"

    async def fake_gemini(_cleaned):
        raise RuntimeError("Gemini TTS 503")

    with (
        patch("src.tools.tts.env.TTS_PROVIDER", "gemini"),
        patch("src.tools.tts._synthesize_google", fake_google),
        patch("src.tools.tts._synthesize_gemini", fake_gemini),
    ):
        result = asyncio.run(generate_audio_response(text="Olá cidadão"))
    assert result["status"] == "ok"
    assert result["voice_used"] == "pt-BR-Neural2-A"  # voz do Google (fallback)
    assert result["audio_base64"] == base64.b64encode(b"OGGGOOGLE").decode("ascii")


def test_gemini_and_google_both_fail_returns_error():
    async def fake_google(_cleaned):
        raise RuntimeError("Google TTS auth fail")

    async def fake_gemini(_cleaned):
        raise RuntimeError("Gemini TTS 503")

    with (
        patch("src.tools.tts.env.TTS_PROVIDER", "gemini"),
        patch("src.tools.tts._synthesize_google", fake_google),
        patch("src.tools.tts._synthesize_gemini", fake_gemini),
    ):
        result = asyncio.run(generate_audio_response(text="Olá cidadão"))
    assert result["status"] == "error"
    assert "Google TTS auth fail" in result["error"]  # a falha do fallback aparece


def test_google_provider_failure_does_not_try_gemini():
    """Fallback é só gemini→google; provider=google que falha não tenta gemini."""

    async def fake_google(_cleaned):
        raise RuntimeError("Google boom")

    async def fake_gemini(_cleaned):
        raise AssertionError("gemini não deve ser tentado quando provider=google")

    with (
        patch("src.tools.tts.env.TTS_PROVIDER", "google"),
        patch("src.tools.tts._synthesize_google", fake_google),
        patch("src.tools.tts._synthesize_gemini", fake_gemini),
    ):
        result = asyncio.run(generate_audio_response(text="Olá cidadão"))
    assert result["status"] == "error"
    assert "Google boom" in result["error"]


# --- _pcm_to_ogg (conversão ffmpeg do PCM do Gemini) -----------------------


def test_pcm_to_ogg_invokes_ffmpeg_with_expected_args():
    captured: dict = {}

    class FakeProc:
        returncode = 0

        async def communicate(self, input=None):
            captured["stdin_bytes"] = input
            return b"FAKE_OGG_BYTES", b""

    async def fake_exec(*args, **_kwargs):
        captured["argv"] = args
        return FakeProc()

    with patch("asyncio.create_subprocess_exec", fake_exec):
        out = asyncio.run(tts_module._pcm_to_ogg(b"RAWPCM"))
    assert out == b"FAKE_OGG_BYTES"
    argv = captured["argv"]
    assert argv[0] == "ffmpeg"
    assert "s16le" in argv
    assert "24000" in argv
    assert "16000" in argv
    assert "libopus" in argv
    assert captured["stdin_bytes"] == b"RAWPCM"


def test_pcm_to_ogg_raises_on_ffmpeg_nonzero_exit():
    class FakeProc:
        returncode = 1

        async def communicate(self, input=None):
            return b"", b"ffmpeg boom"

    async def fake_exec(*_args, **_kwargs):
        return FakeProc()

    with patch("asyncio.create_subprocess_exec", fake_exec):
        with pytest.raises(RuntimeError) as exc:
            asyncio.run(tts_module._pcm_to_ogg(b"RAWPCM"))
    assert "ffmpeg" in str(exc.value).lower()


def test_pcm_to_ogg_raises_on_empty_output():
    class FakeProc:
        returncode = 0

        async def communicate(self, input=None):
            return b"", b""

    async def fake_exec(*_args, **_kwargs):
        return FakeProc()

    with patch("asyncio.create_subprocess_exec", fake_exec):
        with pytest.raises(RuntimeError):
            asyncio.run(tts_module._pcm_to_ogg(b"RAWPCM"))


# --- Caminho completo gemini (client fake + ffmpeg mockado) ----------------


def _make_fake_genai_client(pcm_data: bytes):
    """Constrói (FakeClient, generate_mock) pro caminho Gemini.

    Devolve também o AsyncMock de generate_content pra inspeção dos args
    (modelo, voz, style-prompt prependado). O fake `aio` expõe `aclose`
    (AsyncMock) porque _synthesize_gemini fecha o client no finally.
    """
    inline = SimpleNamespace(data=pcm_data)
    part = SimpleNamespace(inline_data=inline)
    content = SimpleNamespace(parts=[part])
    candidate = SimpleNamespace(content=content)
    response = SimpleNamespace(candidates=[candidate])
    generate = AsyncMock(return_value=response)
    aio = SimpleNamespace(
        models=SimpleNamespace(generate_content=generate),
        aclose=AsyncMock(),
    )

    class FakeClient:
        def __init__(self, *_a, **_kw):
            self.aio = aio

    return FakeClient, generate


def test_gemini_full_path_ok():
    FakeClient, generate = _make_fake_genai_client(pcm_data=b"RAWPCM24K")

    async def fake_pcm_to_ogg(pcm_bytes):
        assert pcm_bytes == b"RAWPCM24K"
        return b"CONVERTED_OGG"

    with (
        patch("src.tools.tts.env.TTS_PROVIDER", "gemini"),
        patch("src.tools.tts.env.TTS_GEMINI_MODEL", "gemini-2.5-flash-preview-tts"),
        patch("src.tools.tts.env.TTS_GEMINI_VOICE", "Sulafat"),
        patch("src.tools.tts.env.TTS_GEMINI_STYLE_PROMPT", "Fale carioca"),
        patch("src.tools.tts.env.GEMINI_API_KEY", "fake-key"),
        patch("src.tools.tts._pcm_to_ogg", fake_pcm_to_ogg),
        patch("google.genai.Client", FakeClient),
    ):
        result = asyncio.run(generate_audio_response(text="Como abrir um chamado?"))
    assert result["status"] == "ok"
    assert result["voice_used"] == "Sulafat"
    assert result["mime_type"] == "audio/ogg"
    assert result["audio_base64"] == base64.b64encode(b"CONVERTED_OGG").decode("ascii")

    # Inspeciona os args passados ao SDK: modelo, style-prompt prependado ao
    # texto, e voz carioca selecionada. Garante que o switch de provider
    # realmente despachou pro Gemini com a config esperada (ver ADR-038).
    generate.assert_awaited_once()
    call_kwargs = generate.await_args.kwargs
    assert call_kwargs["model"] == "gemini-2.5-flash-preview-tts"
    assert call_kwargs["contents"] == "Fale carioca: Como abrir um chamado?"
    voice_cfg = call_kwargs["config"].speech_config.voice_config
    assert voice_cfg.prebuilt_voice_config.voice_name == "Sulafat"


def test_gemini_ffmpeg_missing_falls_back_to_google():
    """ffmpeg ausente faz o Gemini falhar → o fallback entrega o áudio via Google
    (antes isto retornava error; agora a resiliência do #303 cobre o caso).
    `_synthesize_google` é mockado pra o teste ficar hermético (sem rede)."""
    FakeClient, _generate = _make_fake_genai_client(pcm_data=b"RAWPCM24K")

    async def fake_exec(*_args, **_kwargs):
        raise FileNotFoundError("ffmpeg not found")

    async def fake_google(_cleaned):
        return b"OGGGOOGLE", "pt-BR-Neural2-A"

    with (
        patch("src.tools.tts.env.TTS_PROVIDER", "gemini"),
        patch("src.tools.tts.env.GEMINI_API_KEY", "fake-key"),
        patch("google.genai.Client", FakeClient),
        patch("asyncio.create_subprocess_exec", fake_exec),
        patch("src.tools.tts._synthesize_google", fake_google),
    ):
        result = asyncio.run(generate_audio_response(text="Olá"))
    assert result["status"] == "ok"
    assert result["voice_used"] == "pt-BR-Neural2-A"  # voz do Google (fallback)
    assert result["audio_base64"] == base64.b64encode(b"OGGGOOGLE").decode("ascii")


# --- Cache transparente do TTS (Feature 2a) --------------------------------


def test_cache_hit_skips_synthesis():
    """1ª chamada sintetiza + salva; 2ª (mesmo texto) volta do cache sem
    re-sintetizar — todo texto pré-definido fica salvo após o 1º uso."""
    store: dict = {}
    calls = {"synth": 0}

    async def fake_get(key):
        return store.get(key)

    async def fake_set(key, result):
        store[key] = result

    async def fake_google(_cleaned):
        calls["synth"] += 1
        return b"OGGCACHED", "pt-BR-Neural2-A"

    with (
        patch.dict(os.environ, {"TTS_CACHE_ENABLED": "true"}),
        patch("src.tools.tts.env.TTS_PROVIDER", "google"),
        patch("src.tools.tts._synthesize_google", fake_google),
        patch("src.tools.tts._audio_cache_get", fake_get),
        patch("src.tools.tts._audio_cache_set", fake_set),
    ):
        r1 = asyncio.run(generate_audio_response(text="Texto pré-definido"))
        r2 = asyncio.run(generate_audio_response(text="Texto pré-definido"))

    assert r1["status"] == "ok" and r2["status"] == "ok"
    assert r1["audio_base64"] == r2["audio_base64"]
    assert calls["synth"] == 1  # sintetizou só na 1ª; a 2ª veio do cache
    assert len(store) == 1


def test_fallback_audio_is_not_cached():
    """Fallback gemini→google NÃO é cacheado (não persiste a voz degradada se o
    gemini se recuperar)."""
    store: dict = {}

    async def fake_get(key):
        return store.get(key)

    async def fake_set(key, result):
        store[key] = result

    async def fake_google(_cleaned):
        return b"OGGGOOGLE", "pt-BR-Neural2-A"

    async def fake_gemini(_cleaned):
        raise RuntimeError("Gemini TTS 503")

    with (
        patch.dict(os.environ, {"TTS_CACHE_ENABLED": "true"}),
        patch("src.tools.tts.env.TTS_PROVIDER", "gemini"),
        patch("src.tools.tts._synthesize_google", fake_google),
        patch("src.tools.tts._synthesize_gemini", fake_gemini),
        patch("src.tools.tts._audio_cache_get", fake_get),
        patch("src.tools.tts._audio_cache_set", fake_set),
    ):
        result = asyncio.run(generate_audio_response(text="Olá"))

    assert result["status"] == "ok"
    assert store == {}  # fallback não foi salvo


def test_cache_disabled_bypasses_cache():
    """TTS_CACHE_ENABLED=false → não consulta nem grava o cache (sintetiza sempre)."""
    get_mock = AsyncMock(return_value=None)
    set_mock = AsyncMock()

    async def fake_google(_cleaned):
        return b"X", "pt-BR-Neural2-A"

    with (
        patch.dict(os.environ, {"TTS_CACHE_ENABLED": "false"}),
        patch("src.tools.tts.env.TTS_PROVIDER", "google"),
        patch("src.tools.tts._synthesize_google", fake_google),
        patch("src.tools.tts._audio_cache_get", get_mock),
        patch("src.tools.tts._audio_cache_set", set_mock),
    ):
        result = asyncio.run(generate_audio_response(text="A"))

    assert result["status"] == "ok"
    get_mock.assert_not_called()
    set_mock.assert_not_called()


def test_cache_key_stable_and_distinct():
    """Mesma assinatura → mesma chave; texto/provider diferentes → chaves distintas."""
    k1 = tts_module._audio_cache_key("Informe o endereço", "google")
    k2 = tts_module._audio_cache_key("Informe o endereço", "google")
    k3 = tts_module._audio_cache_key("Outro texto", "google")
    k4 = tts_module._audio_cache_key("Informe o endereço", "gemini")
    assert k1 == k2
    assert k1 != k3
    assert k1 != k4
    assert k1.startswith("audio:tts:")


def test_cache_get_degrades_when_redis_down():
    """Redis fora → _audio_cache_get devolve None (degrada gracioso, não levanta)."""

    async def boom(*_a, **_k):
        raise RuntimeError("Redis unreachable")

    with patch("src.utils.redis_client.get_async_redis_client", boom):
        out = asyncio.run(tts_module._audio_cache_get("audio:tts:v1:abc"))
    assert out is None


def test_cache_set_get_roundtrip_with_fake_redis():
    """Cobre o caminho real de _audio_cache_set/_get + _close_redis com um cliente
    Redis fake: set grava JSON, get lê e desserializa, aclose é chamado."""
    store: dict = {}
    closed = {"n": 0}

    class FakeRedis:
        async def get(self, key):
            return store.get(key)

        async def set(self, key, value, ex=None):
            store[key] = value

        async def aclose(self):
            closed["n"] += 1

    async def fake_client(*_a, **_k):
        return FakeRedis()

    payload = {"status": "ok", "audio_base64": "QUJD", "mime_type": "audio/ogg"}
    with patch("src.utils.redis_client.get_async_redis_client", fake_client):
        asyncio.run(tts_module._audio_cache_set("audio:tts:v1:k", payload))
        out = asyncio.run(tts_module._audio_cache_get("audio:tts:v1:k"))

    assert out == payload
    assert closed["n"] == 2  # set + get fecharam o cliente


def test_cache_get_ignores_non_dict_entry():
    """Entrada corrompida (JSON válido mas não-dict) → _audio_cache_get devolve None
    (cai como miss; não quebra o TTS com AttributeError no call site — codex P2)."""

    class FakeRedis:
        async def get(self, key):
            return "[1, 2, 3]"  # JSON válido, porém lista (não dict)

        async def aclose(self):
            pass

    async def fake_client(*_a, **_k):
        return FakeRedis()

    with patch("src.utils.redis_client.get_async_redis_client", fake_client):
        out = asyncio.run(tts_module._audio_cache_get("audio:tts:v1:bad"))
    assert out is None
