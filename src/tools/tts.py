"""
Síntese de áudio (TTS) pra responder cidadão por voz no WhatsApp.

Quando o cidadão pede "responda por áudio" (intent detectado pelo Engine),
o LLM chama esta tool com o texto da resposta. Retorna bytes OGG/Opus
mono 16kHz — formato nativo de PTT do WhatsApp, máxima compatibilidade
com o widget de voice message do app.

Provider switchável via env `TTS_PROVIDER` (ver ADR-038):

  - `google` (default) → Google Cloud Text-to-Speech. Auth via
    GOOGLE_APPLICATION_CREDENTIALS (mesma SA já usada pra Vertex AI /
    GCS / BigQuery). Saída OGG/Opus nativa. Voz pt-BR-Neural2-A
    (feminina, neural); override via env `TTS_VOICE_NAME`. Alternativas:
      - pt-BR-Neural2-A / B / C — neural (alta qualidade)
      - pt-BR-Wavenet-A / B / C / D / E — wavenet (alta qualidade)
      - pt-BR-Standard-A / B / C / D / E — standard (mais barato)

  - `gemini` → Gemini TTS (gemini-2.5-flash-preview-tts). Auth reusa
    GEMINI_API_KEY. A saída do Gemini é PCM raw (s16le, 24kHz, mono, SEM
    header), convertida pra OGG/Opus 16kHz via ffmpeg (subprocess). O
    sotaque carioca é best-effort via style prompt (env
    `TTS_GEMINI_STYLE_PROMPT`) — não há voz dedicada carioca no catálogo
    Gemini. Voz default Sulafat; override via env `TTS_GEMINI_VOICE`.

O contrato de retorno (`AudioResponseResult`) é idêntico entre providers,
então Engine e Mule downstream permanecem provider-agnostic.

Limites:
- WhatsApp PTT inbox limit: 16MB
- Cap implícito por duração: ~16min de áudio @ 16kbps Opus (raramente excedido)
- Texto >5000 chars rejeitado defensivamente (Google API limit em ~5000)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Optional

import src.config.env as env
from src.utils.log import logger


_MAX_TEXT_CHARS = 5000  # Google TTS API hard limit per request
_GEMINI_PCM_SAMPLE_RATE = 24000  # Gemini TTS emite PCM s16le 24kHz mono
_OUTPUT_SAMPLE_RATE = 16000  # OGG/Opus de saída — contrato PTT WhatsApp
_OUTPUT_OPUS_BITRATE = "24k"  # bitrate Opus alvo (voz, não música)
_SUPPORTED_PROVIDERS = ("google", "gemini")  # TTS_PROVIDER aceitos (ver ADR-038)


@dataclass(frozen=True)
class AudioResponseResult:
    """Saída padronizada de generate_audio_response."""

    status: str  # "ok" | "error" | "deferred"
    audio_base64: Optional[str] = None
    mime_type: str = "audio/ogg"
    duration_estimate_s: Optional[float] = None
    voice_used: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        out: dict = {"status": self.status, "mime_type": self.mime_type}
        if self.audio_base64:
            out["audio_base64"] = self.audio_base64
        if self.duration_estimate_s is not None:
            out["duration_estimate_s"] = self.duration_estimate_s
        if self.voice_used:
            out["voice_used"] = self.voice_used
        if self.error:
            out["error"] = self.error
        return out


def _resolve_provider() -> str:
    """Provider TTS ativo; default `google` (backward-compat)."""
    return (env.TTS_PROVIDER or "").strip().lower() or "google"


def _resolve_voice_name() -> str:
    """Default `pt-BR-Neural2-A`; override via env TTS_VOICE_NAME (Google)."""
    return (os.environ.get("TTS_VOICE_NAME") or "").strip() or "pt-BR-Neural2-A"


# ── Cache transparente do TTS (Feature 2a) ──────────────────────────────────
# O áudio é determinístico por (provider, voz, style, texto): a mesma resposta
# pré-definida sintetiza o mesmo OGG. Cacheamos por hash dessa assinatura no Redis
# pra que TODO texto repetido (os templates fixos do bot — e qualquer texto que se
# repita) entregue áudio instantâneo no 2º+ uso, sem re-chamar o TTS. Transparente:
# miss gera+salva, hit retorna o salvo. Best-effort: Redis fora degrada pra geração
# on-demand (nunca quebra o TTS). Kill-switch via env TTS_CACHE_ENABLED=false.
_AUDIO_CACHE_VERSION = "v1"  # bump invalida tudo (troca de voz/provider/formato)
_AUDIO_CACHE_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 dias
_AUDIO_CACHE_KEY_PREFIX = "audio:tts"


def _cache_enabled() -> bool:
    """Cache ON por default; kill-switch via env TTS_CACHE_ENABLED=false."""
    return (os.environ.get("TTS_CACHE_ENABLED") or "").strip().lower() != "false"


def _audio_cache_key(cleaned: str, provider: str) -> str:
    """Chave estável pela assinatura de síntese (provider+voz+style+texto).

    Inclui tudo que altera o OGG de saída, então trocar voz/provider/style gera
    chave nova (não serve áudio velho). A versão no prefixo permite invalidação em
    massa. O texto entra no hash (aceita qualquer tamanho)."""
    if provider == "gemini":
        voice = env.TTS_GEMINI_VOICE or ""
        style = (env.TTS_GEMINI_STYLE_PROMPT or "").strip()
        model = env.TTS_GEMINI_MODEL or ""  # troca de modelo invalida (codex P2)
    else:
        voice = _resolve_voice_name()
        style = ""
        model = ""
    signature = f"{_AUDIO_CACHE_VERSION}|{provider}|{model}|{voice}|{style}|{cleaned}"
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()
    return f"{_AUDIO_CACHE_KEY_PREFIX}:{_AUDIO_CACHE_VERSION}:{digest}"


async def _close_redis(client) -> None:
    """Fecha o cliente async (aclose em redis-py>=5; close como fallback)."""
    try:
        closer = getattr(client, "aclose", None) or getattr(client, "close", None)
        if closer is not None:
            await closer()
    except Exception:
        pass


async def _audio_cache_get(key: str) -> Optional[dict]:
    """Lê o resultado cacheado (dict no formato to_dict). None em miss/erro/Redis fora."""
    try:
        from src.utils.redis_client import get_async_redis_client

        client = await get_async_redis_client()
        try:
            raw = await client.get(key)
        finally:
            await _close_redis(client)
        if raw:
            parsed = json.loads(raw)
            # Best-effort: só trata como hit se for um dict. Uma entrada corrompida/
            # poisoned (JSON válido mas não-objeto) cai como miss em vez de quebrar o
            # TTS lá no call site com AttributeError no .get (codex P2).
            if isinstance(parsed, dict):
                return parsed
            logger.warning(
                "generate_audio_response: cache entry inválida (não-dict); ignorando"
            )
    except Exception as exc:
        logger.warning(
            "generate_audio_response: cache GET falhou "
            f"({type(exc).__name__}: {exc}); seguindo sem cache"
        )
    return None


async def _audio_cache_set(key: str, result: dict) -> None:
    """Salva o resultado (TTL longo). Best-effort: erro só loga warning."""
    try:
        from src.utils.redis_client import get_async_redis_client

        client = await get_async_redis_client()
        try:
            await client.set(key, json.dumps(result), ex=_AUDIO_CACHE_TTL_SECONDS)
        finally:
            await _close_redis(client)
    except Exception as exc:
        logger.warning(
            f"generate_audio_response: cache SET falhou ({type(exc).__name__}: {exc})"
        )


async def _synthesize_google(cleaned: str) -> tuple[bytes, str]:
    """Sintetiza via Google Cloud TTS; retorna (ogg_bytes, voice_name)."""
    # Import lazy — google-cloud-texttospeech só puxa quando a tool é
    # de fato chamada. Evita peso de import desnecessário em todos os
    # processos do MCP que nunca rodam TTS.
    from google.cloud import texttospeech

    voice_name = _resolve_voice_name()

    # Cliente é fechado explicitamente após cada call. O SDK gerencia
    # o gRPC channel internamente; sem close(), em processos
    # long-lived (MCP server roda dias) os channels acumulam e
    # eventualmente esgotam FDs / quota de conexões. Codex P2 2026-05-15.
    client = texttospeech.TextToSpeechAsyncClient()

    try:
        synth_input = texttospeech.SynthesisInput(text=cleaned)
        voice = texttospeech.VoiceSelectionParams(
            language_code="pt-BR",
            name=voice_name,
        )
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.OGG_OPUS,
            sample_rate_hertz=_OUTPUT_SAMPLE_RATE,
            # Speaking rate 1.0 = natural. Aumentar > 1.0 acelera.
            speaking_rate=1.0,
        )

        response = await client.synthesize_speech(
            input=synth_input,
            voice=voice,
            audio_config=audio_config,
        )
    finally:
        # close() libera channels gRPC. Em google-cloud >= 2.0,
        # AsyncClient expõe close() via transport.
        try:
            await client.transport.close()
        except Exception:
            # Defensivo: se transport API mudar entre versões, não
            # quebra a síntese — apenas log warning.
            logger.warning(
                "generate_audio_response: nao foi possivel fechar "
                "TextToSpeechAsyncClient (transport API drift)"
            )

    return response.audio_content, voice_name


async def _pcm_to_ogg(pcm_bytes: bytes) -> bytes:
    """
    Converte PCM raw (s16le 24kHz mono) → OGG/Opus 16kHz via ffmpeg.

    Usa `create_subprocess_exec` (não shell) com args em lista: nenhum
    input do usuário entra na linha de comando — os bytes PCM vão por
    stdin, então não há superfície de command injection. O Gemini TTS
    entrega PCM sem header de container; ffmpeg precisa dos parâmetros
    explícitos (-f s16le -ar 24000 -ac 1) pra interpretar o stream.
    """
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "s16le",
        "-ar",
        str(_GEMINI_PCM_SAMPLE_RATE),
        "-ac",
        "1",
        "-i",
        "pipe:0",
        "-c:a",
        "libopus",
        "-b:a",
        _OUTPUT_OPUS_BITRATE,
        "-ar",
        str(_OUTPUT_SAMPLE_RATE),
        "-f",
        "ogg",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate(input=pcm_bytes)

    if process.returncode != 0:
        detail = (stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"ffmpeg PCM->OGG falhou (rc={process.returncode}): {detail}"
        )
    if not stdout:
        raise RuntimeError("ffmpeg PCM->OGG produziu saída vazia")

    return stdout


async def _synthesize_gemini(cleaned: str) -> tuple[bytes, str]:
    """
    Sintetiza via Gemini TTS; retorna (ogg_bytes, voice_name).

    Gemini emite PCM raw — converte pra OGG/Opus via _pcm_to_ogg. Sotaque
    carioca é best-effort prependando o style prompt ao texto.
    """
    # Import lazy — google-genai só puxa quando provider=gemini.
    from google import genai
    from google.genai import types

    voice_name = env.TTS_GEMINI_VOICE
    style_prompt = (env.TTS_GEMINI_STYLE_PROMPT or "").strip()
    contents = f"{style_prompt}: {cleaned}" if style_prompt else cleaned

    client = genai.Client(api_key=env.GEMINI_API_KEY)
    config = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=voice_name,
                )
            )
        ),
    )

    # Fecha o async client após cada call. genai.Client().aio é um
    # AsyncClient com aclose() (expõe aclose, não close); sem ele, em
    # processos long-lived (MCP server roda dias) os httpx channels
    # acumulam e esgotam FDs. Mesmo motivo do transport.close() no
    # provider Google. Codex P2 2026-05-25.
    try:
        response = await client.aio.models.generate_content(
            model=env.TTS_GEMINI_MODEL,
            contents=contents,
            config=config,
        )

        pcm_bytes = response.candidates[0].content.parts[0].inline_data.data
        if not pcm_bytes:
            raise RuntimeError("Gemini TTS retornou áudio vazio")
    finally:
        try:
            await client.aio.aclose()
        except Exception:
            # Defensivo: se a API de close mudar entre versões do SDK,
            # não quebra a síntese — apenas log warning.
            logger.warning(
                "generate_audio_response: nao foi possivel fechar "
                "genai AsyncClient (aclose API drift)"
            )

    ogg_bytes = await _pcm_to_ogg(pcm_bytes)
    return ogg_bytes, voice_name


async def _synthesize_with_fallback(
    provider: str, cleaned: str
) -> tuple[bytes, str, str]:
    """Sintetiza no provider configurado; retorna (ogg_bytes, voice, provider_efetivo).

    Resiliência (POC1 acessibilidade): se o provider for ``gemini`` e ele falhar
    (API/modelo/credencial/ffmpeg), cai automaticamente pra ``google`` — num
    serviço público, entregar áudio com a voz padrão é melhor que não entregar
    nada pra quem não lê. O fallback é só gemini→google (google é o default
    robusto, sem dependência de ffmpeg); não volta pro gemini. Não mascara a
    falha: loga warning com a causa, e o ``provider_efetivo`` retornado a
    registra (ex.: ``google(fallback)``).
    """
    if provider == "gemini":
        try:
            audio_bytes, voice_name = await _synthesize_gemini(cleaned)
            return audio_bytes, voice_name, "gemini"
        except Exception as exc:
            logger.warning(
                "generate_audio_response: provider gemini falhou "
                f"({type(exc).__name__}: {exc}); fallback pra google"
            )
            audio_bytes, voice_name = await _synthesize_google(cleaned)
            return audio_bytes, voice_name, "google(fallback)"

    audio_bytes, voice_name = await _synthesize_google(cleaned)
    return audio_bytes, voice_name, "google"


async def generate_audio_response(text: str) -> dict:
    """
    Sintetiza texto em audio OGG/Opus 16kHz mono pra responder cidadão por
    voz no WhatsApp. Provider escolhido via env TTS_PROVIDER (ver ADR-038).

    Args:
        text: Texto a ser sintetizado em PT-BR. Recomendado <=2000 chars
            (10-12s de áudio); aceita até 5000 chars (limite Google TTS).

    Returns:
        Dict com:
        - status: "ok" / "error" / "deferred"
        - audio_base64: bytes do OGG codificados em base64 (se status=ok)
        - mime_type: "audio/ogg" (Opus codec)
        - duration_estimate_s: estimativa baseada em ~15 chars/seg
        - voice_used: identificador da voz (ex: "pt-BR-Neural2-A", "Sulafat")
        - error: descritivo se status != ok

    Erros comuns:
    - credenciais ausentes (Google ADC / GEMINI_API_KEY) → status=error
    - texto vazio ou >5000 chars → status=error
    - quota/network/ffmpeg → status=error (logger registra detalhe)
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return AudioResponseResult(status="error", error="texto vazio").to_dict()
    if len(cleaned) > _MAX_TEXT_CHARS:
        return AudioResponseResult(
            status="error",
            error=f"texto excede {_MAX_TEXT_CHARS} chars (Google TTS API limit)",
        ).to_dict()

    # NOTA: NÃO pre-checa credenciais (GOOGLE_APPLICATION_CREDENTIALS /
    # GEMINI_API_KEY) porque o Google ADC também funciona via metadata
    # server (GKE/Cloud Run) ou ~/.config/gcloud sem env var. Deixa o
    # cliente falhar com auth error se nenhuma source estiver disponível —
    # o exception handler abaixo captura e retorna status='error'. Codex P2.
    provider = _resolve_provider()
    if provider not in _SUPPORTED_PROVIDERS:
        # Provider desconhecido é erro de configuração, não motivo pra
        # cair silenciosamente no Google — o operador setou TTS_PROVIDER
        # errado e precisa saber. Antes isto degradava pra google sem aviso.
        return AudioResponseResult(
            status="error",
            error=(
                f"TTS_PROVIDER inválido: {provider!r} "
                f"(suportados: {', '.join(_SUPPORTED_PROVIDERS)})"
            ),
        ).to_dict()

    # Cache transparente (Feature 2a): hit → devolve o áudio salvo sem re-sintetizar.
    cache_key = _audio_cache_key(cleaned, provider) if _cache_enabled() else None
    if cache_key is not None:
        cached = await _audio_cache_get(cache_key)
        if (
            isinstance(cached, dict)
            and cached.get("status") == "ok"
            and cached.get("audio_base64")
        ):
            logger.info(
                f"generate_audio_response: cache HIT provider={provider} "
                f"text_len={len(cleaned)}"
            )
            return cached

    try:
        audio_bytes, voice_name, provider_used = await _synthesize_with_fallback(
            provider, cleaned
        )

        audio_b64 = base64.b64encode(audio_bytes).decode("ascii")

        # ~15 chars/seg PT-BR speaking rate baseline (rough heuristic
        # pra Mule estimar bandwidth/timeout sem decodar bytes).
        duration = max(1.0, len(cleaned) / 15.0)

        logger.info(
            f"generate_audio_response: provider={provider_used} "
            f"text_len={len(cleaned)} audio_bytes={len(audio_bytes)} "
            f"voice={voice_name} duration_est={duration:.1f}s"
        )

        result = AudioResponseResult(
            status="ok",
            audio_base64=audio_b64,
            mime_type="audio/ogg",
            duration_estimate_s=duration,
            voice_used=voice_name,
        ).to_dict()

        # Salva no cache só a síntese LIMPA — não o fallback degradado
        # (provider_used="google(fallback)"), pra não persistir a voz errada se o
        # gemini se recuperar depois.
        if cache_key is not None and provider_used == provider:
            await _audio_cache_set(cache_key, result)

        return result

    except Exception as e:
        logger.exception(
            f"generate_audio_response: erro inesperado ao sintetizar "
            f"(provider={provider}, {type(e).__name__}: {e})"
        )
        return AudioResponseResult(
            status="error",
            error=f"{type(e).__name__}: {e}",
        ).to_dict()
