FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app

# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Manifestos primeiro: a camada de dependências só é reconstruída quando o
# lock muda, não a cada alteração de código.
COPY pyproject.toml uv.lock README.md /app/

# `--frozen` falha se o lock estiver dessincronizado do pyproject, em vez de
# resolver silenciosamente algo diferente do que o CI testou.
# `--no-install-project` porque o código ainda não foi copiado.
RUN uv sync --frozen --no-dev --no-install-project

COPY . /app

RUN uv sync --frozen --no-dev

EXPOSE 80

CMD ["uv", "run", "python", "-m", "src.main"]
