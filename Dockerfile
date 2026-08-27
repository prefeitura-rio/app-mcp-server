# ==== build ==================================================================
# `build-essential` vive só nesta etapa. Ele arrasta `linux-libc-dev` e a
# árvore do `perl`, que sozinhos respondiam pela maior parte dos alertas de OS
# da imagem final — e nenhum dos dois tem correção publicada no Debian, então
# a única saída é não embarcá-los.
FROM python:3.12-slim AS build

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

# ==== runtime ================================================================
FROM python:3.12-slim AS runtime

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app

# `upgrade` aplica as correções já publicadas para os pacotes do base image
# (hoje: openssl). O `setuptools` que vem no base image não é usado em runtime
# — o venv do projeto não o instala — mas o scanner o enxerga na imagem, daí
# subi-lo em vez de removê-lo: mais barato que descobrir na produção que
# alguma lib ainda importa `pkg_resources`.
# hadolint ignore=DL3005,DL3008,DL3013
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --upgrade "setuptools>=78.1.1"

# O venv tem shebangs e caminhos absolutos gravados no `uv sync`: precisa
# desembarcar exatamente no mesmo `/app` da etapa de build.
COPY --from=build /app /app

EXPOSE 80

# `--no-sync`: as dependências já vieram prontas da etapa de build, e sem
# `build-essential` aqui uma re-resolução silenciosa não teria como compilar
# nada — melhor falhar na build que na partida do pod.
CMD ["uv", "run", "--no-sync", "python", "-m", "src.main"]
