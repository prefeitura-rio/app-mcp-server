FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app

RUN apt-get update && apt-get install -y build-essential && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ADD . /app

RUN uv sync

EXPOSE 80
    
CMD ["uv", "run", "python", "-m", "src.main"] 