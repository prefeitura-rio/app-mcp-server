"""
Testes do encurtador de URL compartilhado.

O encurtador é o último passo de todo link que vai ao cidadão (página Pix, PDF
do DARM). Falha aqui não pode derrubar a emissão: cada chamador tem a URL
original como fallback, e por isso a função devolve `None` em vez de propagar.
"""

import datetime as dt

import httpx
import pytest

import src.utils.short_url as short_url_mod
from src.utils.short_url import format_expires_at, get_short_url


SOURCE = {"source": "mcp", "tool": "multi_step_service", "workflow": "iptu_pagamento"}


@pytest.fixture
def env_encurtador(monkeypatch):
    import types

    env_module = types.SimpleNamespace(
        SHORT_API_URL="https://pref.rio",
        SHORT_API_TOKEN="short-token",
    )
    monkeypatch.setattr(short_url_mod, "env", env_module)
    return env_module


class FakeResponse:
    def __init__(self, status_code=201, payload=None):
        self.status_code = status_code
        self._payload = payload or {"short_path": "abc123"}

    def json(self):
        return self._payload


def fake_client_factory(captured=None, response=None, error=None):
    class FakeClient:
        def __init__(self, **kwargs):
            if captured is not None:
                captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, json=None, headers=None):
            if captured is not None:
                captured["url"] = url
                captured["payload"] = json
                captured["headers"] = headers
            if error:
                raise error
            return response or FakeResponse()

    return FakeClient


def test_format_expires_at_converte_para_utc():
    expiration = dt.datetime(
        2026, 5, 11, 9, 30, 15, 123456, tzinfo=dt.timezone(dt.timedelta(hours=-3))
    )

    assert format_expires_at(expiration) == "2026-05-11T12:30:15Z"


def test_format_expires_at_recusa_datetime_sem_timezone():
    """Sem tzinfo, o vencimento do link dependeria do TZ do container."""
    with pytest.raises(ValueError):
        format_expires_at(dt.datetime(2026, 5, 11, 9, 30, 15))


@pytest.mark.asyncio
async def test_encurta_e_monta_payload_completo(monkeypatch, env_encurtador):
    captured = {}
    monkeypatch.setattr(
        short_url_mod, "InterceptedHTTPClient", fake_client_factory(captured)
    )

    short_url = await get_short_url(
        url="https://storage.example/signed",
        title="Titulo",
        description="Descricao",
        user_id="user-1",
        source=SOURCE,
        expires_at="2026-05-11T12:00:00Z",
        image_url="https://example.com/image.png",
        short_path="meu-link",
    )

    assert short_url == "https://pref.rio/link/abc123"
    assert captured["url"] == "https://pref.rio/link/api/urls"
    assert captured["headers"]["Authorization"] == "Bearer short-token"
    assert captured["payload"] == {
        "description": "Descricao",
        "destination": "https://storage.example/signed",
        "expires_at": "2026-05-11T12:00:00Z",
        "image_url": "https://example.com/image.png",
        "short_path": "meu-link",
        "title": "Titulo",
    }


@pytest.mark.asyncio
async def test_campos_opcionais_ficam_fora_do_payload(monkeypatch, env_encurtador):
    captured = {}
    monkeypatch.setattr(
        short_url_mod, "InterceptedHTTPClient", fake_client_factory(captured)
    )

    await get_short_url(
        url="https://storage.example/signed",
        title="Titulo",
        description="Descricao",
        user_id="user-1",
        source=SOURCE,
    )

    assert captured["payload"] == {
        "description": "Descricao",
        "destination": "https://storage.example/signed",
        "title": "Titulo",
    }


@pytest.mark.asyncio
async def test_source_e_user_id_chegam_ao_interceptor(monkeypatch, env_encurtador):
    captured = {}
    monkeypatch.setattr(
        short_url_mod, "InterceptedHTTPClient", fake_client_factory(captured)
    )

    await get_short_url(
        url="https://storage.example/signed",
        title="Titulo",
        description="Descricao",
        user_id="user-1",
        source=SOURCE,
    )

    assert captured["client_kwargs"]["user_id"] == "user-1"
    assert captured["client_kwargs"]["source"] == SOURCE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response, error",
    [
        (FakeResponse(status_code=500), None),
        (None, httpx.TimeoutException("timeout")),
        (None, RuntimeError("boom")),
    ],
    ids=["status-de-erro", "timeout", "excecao-generica"],
)
async def test_falha_devolve_none(monkeypatch, env_encurtador, response, error):
    monkeypatch.setattr(
        short_url_mod,
        "InterceptedHTTPClient",
        fake_client_factory(response=response, error=error),
    )

    assert (
        await get_short_url(
            url="url",
            title="title",
            description="description",
            user_id="user-1",
            source=SOURCE,
        )
        is None
    )


@pytest.mark.asyncio
async def test_link_da_guia_nao_vai_para_o_log(monkeypatch, env_encurtador):
    """
    CHATR-174 D3: a resposta do encurtador traz o `short_path`, ou seja, o link
    direto para a guia daquele contribuinte. Quem lê o log acessa o documento.
    """
    monkeypatch.setattr(
        short_url_mod,
        "InterceptedHTTPClient",
        fake_client_factory(response=FakeResponse(payload={"short_path": "s3gr3d0"})),
    )

    registros = []
    sink_id = short_url_mod.logger.add(registros.append, level="DEBUG")
    try:
        short_url = await get_short_url(
            url="https://storage.example/signed",
            title="Titulo",
            description="Descricao",
            user_id="user-1",
            source=SOURCE,
        )
    finally:
        short_url_mod.logger.remove(sink_id)

    # O link continua sendo devolvido a quem chamou — só não é logado
    assert short_url == "https://pref.rio/link/s3gr3d0"
    assert "s3gr3d0" not in "".join(registros)
