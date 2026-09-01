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


async def _app_que_engole(scope, receive, send):
    """Handler no padrão do `src/app.py`: `except Exception` em volta da leitura.

    É o formato dos cinco handlers de Dívida Ativa. Enquanto o sentinel herdava
    de `Exception`, ele era engolido aqui e a resposta virava 500.
    """
    try:
        while True:
            mensagem = await receive()
            if mensagem["type"] != "http.request":
                break
            if not mensagem.get("more_body"):
                break
        status, corpo = 200, b"ok"
    except Exception as erro:  # noqa: BLE001
        status, corpo = 500, f'{{"error":"{erro}"}}'.encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": corpo})


async def _app_com_task_group(scope, receive, send):
    """Lê o corpo de dentro de um task group do anyio.

    É o que o transporte MCP em streaming faz. O anyio entrega a exceção da
    tarefa filha dentro de um `BaseExceptionGroup`, e um `except` seco pelo
    tipo do sentinel não pegaria.
    """
    import anyio

    async def ler():
        while True:
            mensagem = await receive()
            if mensagem["type"] != "http.request":
                break
            if not mensagem.get("more_body"):
                break

    async with anyio.create_task_group() as tg:
        tg.start_soon(ler)

    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": b"ok"})


async def _corpo_em_chunks(total, pedaco=32):
    enviados = 0
    while enviados < total:
        atual = min(pedaco, total - enviados)
        yield b"a" * atual
        enviados += atual


@pytest.mark.asyncio
async def test_handler_que_engole_a_excecao_ainda_recebe_413():
    """O teto não pode depender de o handler colaborar.

    Este é o teste do defeito que a revisão do PR #169 pegou: `except Exception`
    no handler transformava o estouro em 500 com corpo vazio, e nem o 413 nem a
    telemetria saíam.
    """
    app = LimitRequestBodyMiddleware(_app_que_engole, max_bytes=100)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/x", content=_corpo_em_chunks(500))

    assert r.status_code == 413
    assert r.json()["error"] == "payload_too_large"


@pytest.mark.asyncio
async def test_sentinel_embrulhado_em_grupo_ainda_vira_413():
    """Sentinel atravessando task group chega como `BaseExceptionGroup`."""
    app = LimitRequestBodyMiddleware(_app_com_task_group, max_bytes=100)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/x", content=_corpo_em_chunks(500))

    assert r.status_code == 413


@pytest.mark.asyncio
async def test_excecao_alheia_nao_e_engolida():
    """`except BaseException` no middleware não pode virar um sumidouro."""

    async def _app_que_quebra(scope, receive, send):
        await receive()
        raise RuntimeError("falha de verdade")

    app = LimitRequestBodyMiddleware(_app_que_quebra, max_bytes=100)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        with pytest.raises(RuntimeError, match="falha de verdade"):
            await c.post("/x", content=b"a")
