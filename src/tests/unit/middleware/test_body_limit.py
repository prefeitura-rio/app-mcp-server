"""Teto de corpo de requisição — os dois caminhos, declarado e em chunks."""

import httpx
import pytest

from src.middleware.body_limit import LimitRequestBodyMiddleware


async def _app_alvo(scope, receive, send):
    """Lê o corpo inteiro, como `await request.json()` faz, e conta os bytes."""
    corpo = b""
    while True:
        mensagem = await receive()
        if mensagem["type"] != "http.request":
            break
        corpo += mensagem.get("body") or b""
        if not mensagem.get("more_body"):
            break
    _app_alvo.bytes_lidos = len(corpo)
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": b"ok"})


@pytest.fixture(autouse=True)
def _zera():
    _app_alvo.bytes_lidos = None
    yield


def _cliente(max_bytes=100):
    app = LimitRequestBodyMiddleware(_app_alvo, max_bytes=max_bytes)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


@pytest.mark.asyncio
async def test_corpo_dentro_do_teto_passa():
    async with _cliente() as c:
        r = await c.post("/x", content=b"a" * 50)
    assert r.status_code == 200
    assert _app_alvo.bytes_lidos == 50


@pytest.mark.asyncio
async def test_content_length_acima_do_teto_recusa_sem_ler():
    """O caminho barato: recusa antes de o corpo virar memória."""
    async with _cliente() as c:
        r = await c.post("/x", content=b"a" * 500)
    assert r.status_code == 413
    assert r.json()["error"] == "payload_too_large"
    assert _app_alvo.bytes_lidos is None


@pytest.mark.asyncio
async def test_sem_content_length_a_contagem_corta():
    """O caminho que de fato limita a memória: `Content-Length` é só uma
    promessa do cliente, e um cliente hostil manda em chunks sem declarar."""

    async def corpo_em_chunks():
        for _ in range(10):
            yield b"a" * 50

    async with _cliente() as c:
        r = await c.post("/x", content=corpo_em_chunks())
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_content_length_ilegivel_nao_recusa_sozinho():
    """Header malformado não decide nada aqui — quem decide é a contagem."""
    async with _cliente() as c:
        r = await c.post("/x", content=b"a" * 10, headers={"Content-Length": "abc"})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_get_sem_corpo_passa():
    async with _cliente() as c:
        r = await c.get("/health")
    assert r.status_code == 200
