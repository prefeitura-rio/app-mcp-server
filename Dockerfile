FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app

# ffmpeg: usado pelo TTS provider=gemini pra converter PCM raw (s16le 24kHz)
# em OGG/Opus. Sem ele, provider=gemini falha; provider=google (default)
# não depende de ffmpeg. Ver ADR-038.
RUN apt-get update && apt-get install -y --no-install-recommends build-essential ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY . /app

RUN uv sync

EXPOSE 80
    
CMD ["uv", "run", "python", "-m", "src.main"] 
