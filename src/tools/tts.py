"""
Síntese de áudio (TTS) pra responder cidadão por voz no WhatsApp.

Quando o cidadão pede "responda por áudio" (intent detectado pelo Engine),
o LLM chama esta tool com o texto da resposta. Retorna bytes OGG/Opus
mono 16kHz — formato nativo de PTT do WhatsApp, máxima compatibilidade
com o widget de voice message do app.

Provider: Google Cloud Text-to-Speech. Auth via GOOGLE_APPLICATION_CREDENTIALS
(mesma SA já usada pra Vertex AI / GCS / BigQuery).

Voz: pt-BR-Neural2-A (feminina, neural quality). Alternativas para
configurar via env var TTS_VOICE_NAME:
  - pt-BR-Neural2-A / B / C — neural (alta qualidade)
  - pt-BR-Wavenet-A / B / C / D / E — wavenet (alta qualidade)
  - pt-BR-Standard-A / B / C / D / E — standard (mais barato)

Limites:
- WhatsApp PTT inbox limit: 16MB
- Cap implícito por duração: ~16min de áudio @ 16kbps Opus (raramente excedido)
- Texto >5000 chars rejeitado defensivamente (Google API limit em ~5000)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from src.utils.log import logger


_MAX_TEXT_CHARS = 5000  # Google TTS API hard limit per request


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


def _resolve_voice_name() -> str:
    """Default `pt-BR-Neural2-A`; override via env TTS_VOICE_NAME."""
    return (os.environ.get("TTS_VOICE_NAME") or "").strip() or "pt-BR-Neural2-A"


async def generate_audio_response(text: str) -> dict:
    """
    Sintetiza texto em audio OGG/Opus 16kHz mono pra responder cidadão por
    voz no WhatsApp.

    Args:
        text: Texto a ser sintetizado em PT-BR. Recomendado <=2000 chars
            (10-12s de áudio); aceita até 5000 chars (limite Google TTS).

    Returns:
        Dict com:
        - status: "ok" / "error" / "deferred"
        - audio_base64: bytes do OGG codificados em base64 (se status=ok)
        - mime_type: "audio/ogg" (Opus codec)
        - duration_estimate_s: estimativa baseada em ~15 chars/seg
        - voice_used: identificador da voz (ex: "pt-BR-Neural2-A")
        - error: descritivo se status != ok

    Erros comuns:
    - GOOGLE_APPLICATION_CREDENTIALS não setada → status=deferred
    - texto vazio ou >5000 chars → status=error
    - quota/network → status=error (logger registra detalhe)
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return AudioResponseResult(status="error", error="texto vazio").to_dict()
    if len(cleaned) > _MAX_TEXT_CHARS:
        return AudioResponseResult(
            status="error",
            error=f"texto excede {_MAX_TEXT_CHARS} chars (Google TTS API limit)",
        ).to_dict()

    # NOTA: NÃO pre-checa GOOGLE_APPLICATION_CREDENTIALS / GOOGLE_CLOUD_PROJECT
    # porque ADC (Application Default Credentials) também funciona via
    # metadata server (GKE/Cloud Run) ou ~/.config/gcloud/application_default
    # sem nenhuma env var. Deixa o cliente Google falhar com auth error se
    # nenhuma source de credentials estiver disponível — exception handler
    # abaixo captura e retorna `status='error'` com detalhe. Codex P2
    # 2026-05-15.

    voice_name = _resolve_voice_name()

    try:
        # Import lazy — google-cloud-texttospeech só puxa quando a tool é
        # de fato chamada. Evita peso de import desnecessário em todos os
        # processos do MCP que nunca rodam TTS.
        from google.cloud import texttospeech

        # Cliente é fechado explicitamente após cada call. O SDK gerencia
        # o gRPC channel internamente; sem close(), em processos
        # long-lived (MCP server roda dias) os channels acumulam e
        # eventualmente esgotam FDs / quota de conexões. Codex P2
        # 2026-05-15.
        client = texttospeech.TextToSpeechAsyncClient()

        try:
            synth_input = texttospeech.SynthesisInput(text=cleaned)
            voice = texttospeech.VoiceSelectionParams(
                language_code="pt-BR",
                name=voice_name,
            )
            audio_config = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.OGG_OPUS,
                sample_rate_hertz=16000,
                # Speaking rate 1.0 = natural. Aumentar > 1.0 acelera.
                speaking_rate=1.0,
            )

            response = await client.synthesize_speech(
                input=synth_input,
                voice=voice,
                audio_config=audio_config,
            )
        finally:
            # close() é sync (libera channels gRPC). Em google-cloud
            # >= 2.0, AsyncClient expõe close() sem async helper.
            try:
                await client.transport.close()
            except Exception:
                # Defensivo: se transport API mudar entre versões, não
                # quebra a síntese — apenas log warning.
                logger.warning(
                    "generate_audio_response: nao foi possivel fechar "
                    "TextToSpeechAsyncClient (transport API drift)"
                )

        audio_bytes = response.audio_content
        import base64

        audio_b64 = base64.b64encode(audio_bytes).decode("ascii")

        # ~15 chars/seg PT-BR speaking rate baseline (rough heuristic
        # pra Mule estimar bandwidth/timeout sem decodar bytes).
        duration = max(1.0, len(cleaned) / 15.0)

        logger.info(
            f"generate_audio_response: text_len={len(cleaned)} "
            f"audio_bytes={len(audio_bytes)} voice={voice_name} "
            f"duration_est={duration:.1f}s"
        )

        return AudioResponseResult(
            status="ok",
            audio_base64=audio_b64,
            mime_type="audio/ogg",
            duration_estimate_s=duration,
            voice_used=voice_name,
        ).to_dict()

    except Exception as e:
        logger.exception(
            f"generate_audio_response: erro inesperado ao sintetizar "
            f"({type(e).__name__}: {e})"
        )
        return AudioResponseResult(
            status="error",
            error=f"{type(e).__name__}: {e}",
        ).to_dict()
