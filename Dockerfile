# ==== build ==================================================================
# `python:3.11-slim` e não 3.12: o `.python-version` do projeto fixa 3.11, que
# é o que o CI testa. Com 3.12 no base image o uv ignorava esse Python e
# baixava um CPython 3.11 próprio, deixando dois interpretadores na imagem —
# e o do venv morava no cache do uv, fora do `/app`.
FROM python:3.11-slim AS build

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Sem isto o uv volta a baixar o próprio interpretador: o venv precisa apontar
# para `/usr/local/bin/python3.11`, que é o único caminho que também existe na
# etapa de runtime.
ENV UV_PYTHON_PREFERENCE=only-system

WORKDIR /app

# `build-essential` vive só nesta etapa. Ele arrasta `linux-libc-dev` (toda a
# fileira de CVE de kernel) e a árvore do `perl` (os CRITICAL) — justamente os
# pacotes que não têm correção publicada no Debian, então a única saída é não
# embarcá-los na imagem final.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Manifestos primeiro: a camada de dependências só é reconstruída quando o
# lock muda, não a cada alteração de código.
COPY pyproject.toml uv.lock .python-version README.md /app/

# `--frozen` falha se o lock estiver dessincronizado do pyproject, em vez de
# resolver silenciosamente algo diferente do que o CI testou.
# `--no-install-project` porque o código ainda não foi copiado.
RUN uv sync --frozen --no-dev --no-install-project

COPY . /app

RUN uv sync --frozen --no-dev

# ==== runtime ================================================================
FROM python:3.11-slim AS runtime

WORKDIR /app

# `upgrade` aplica as correções já publicadas para os pacotes do base image
# (hoje: openssl). O `setuptools` do base image não é usado em runtime — o
# venv do projeto não o instala — mas o scanner o enxerga na imagem, daí
# subi-lo em vez de removê-lo: mais barato que descobrir em produção que
# alguma lib ainda importa `pkg_resources`.
# hadolint ignore=DL3005,DL3008,DL3013
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --upgrade "setuptools>=78.1.1"

# O venv carrega o caminho absoluto do interpretador: precisa desembarcar no
# mesmo `/app`, e o `/usr/local/bin/python3.11` que ele referencia é o desta
# imagem — mesma base da etapa de build.
COPY --from=build /app /app

# Falha na build, e não na partida do pod, se o venv não tiver sobrevivido à
# cópia entre estágios. Foi exatamente esse o modo de falha quando as duas
# etapas usavam interpretadores diferentes.
RUN .venv/bin/python -c "import pytz, fastmcp, shapely; print('venv OK')"

EXPOSE 80

# Chama o interpretador do venv direto: `uv run` recriaria o ambiente se o
# achasse inconsistente, trocando um erro de build por um pod que não sobe.
# O runtime não precisa do uv — nada no código o invoca.
CMD [".venv/bin/python", "-m", "src.main"]
