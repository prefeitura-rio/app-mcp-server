"""
Testes da DEV-340: a geolocalização do alerta do COR não manda o endereço
digitado pelo cidadão nem as coordenadas exatas para o log.

Antes, `create_cor_alert` logava `Geolocalizando endereço: {address}` e, na
falha, `Não foi possível geolocalizar o endereço: {address}`; o sucesso levava
`lat`/`lng` exatas. Os `warning` de erro do Google Maps levavam `str(e)`, que no
`httpx.HTTPStatusError` traz a URL da requisição: o endereço (geocode), a
coordenada (reverse geocode) e a chave da API. A redação global por padrão não
alcança endereço sem formato nem rótulo (CHATR-167).

Invariantes cobertos aqui:
- nenhuma linha de log da tool (sucesso, sucesso via reverse geocoding, falha
  sem resultado, erro HTTP no geocode e no reverse) contém trecho do endereço,
  do endereço formatado pelo Google, das coordenadas ou da chave;
- o log registra só metadado: `resumir_texto_livre` do endereço, geolocalizado
  sim/não e o bairro normalizado ou `nao_identificado`.
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import httpx
import pytest

from src.utils.pii import resumir_texto_livre


PROJECT_ROOT = Path(__file__).resolve().parents[4]

# Dados sintéticos.
ENDERECO = "Rua Sintetica das Acacias 8765 casa dos fundos"
ENDERECO_FORMATADO = "R. Sintetica das Acacias, 8765 - Rio de Janeiro - RJ"
BAIRRO = "Vila Fictícia"
BAIRRO_NORMALIZADO = "vila ficticia"
LATITUDE = -22.9876543
LONGITUDE = -43.1234567
CHAVE = "chave-sintetica-dev340"
TRECHOS_PROIBIDOS = (
    "Sintetica",
    "Acacias",
    "8765",
    "fundos",
    "22.98",
    "43.12",
    "9876543",
    "1234567",
    CHAVE,
)


def _ensure_package(name: str, path: Path) -> None:
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(path)]
    sys.modules[name] = pkg


def _passthrough_interceptor(*_args, **_kwargs):
    def decorator(func):
        return func

    return decorator


class _LoggerCapturado:
    """Guarda toda chamada de log, em qualquer nível, com os argumentos."""

    def __init__(self):
        self.registros = []

    def __getattr__(self, nivel):
        def _registrar(msg, *args, **kwargs):
            self.registros.append((nivel, str(msg), repr(args), repr(kwargs)))

        return _registrar

    def texto(self) -> str:
        return "\n".join(" ".join(r) for r in self.registros)


def _resposta(payload: dict):
    return types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)


def _resposta_com_erro_http(url: str, params: dict):
    """Resposta real do httpx, cujo `HTTPStatusError` cita a URL com os params."""
    request = httpx.Request("GET", url, params=params)
    response = httpx.Response(400, request=request, json={})
    return types.SimpleNamespace(
        raise_for_status=response.raise_for_status, json=lambda: {}
    )


def _geocode_ok(com_bairro: bool) -> dict:
    componentes = (
        [{"long_name": BAIRRO, "types": ["sublocality_level_1"]}] if com_bairro else []
    )
    return {
        "status": "OK",
        "results": [
            {
                "formatted_address": ENDERECO_FORMATADO,
                "geometry": {"location": {"lat": LATITUDE, "lng": LONGITUDE}},
                "address_components": componentes,
            }
        ],
    }


REVERSE_OK = {
    "status": "OK",
    "results": [
        {"address_components": [{"long_name": BAIRRO, "types": ["neighborhood"]}]}
    ],
}


def _carregar(monkeypatch, alias: str, responder):
    """Carrega `cor_alert_tools.py` com env sintético e o HTTP do Google trocado.

    `responder(url, params)` devolve a resposta de cada GET ao Google Maps.
    """
    _ensure_package("src", PROJECT_ROOT / "src")
    _ensure_package("src.tools", PROJECT_ROOT / "src" / "tools")
    _ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")
    _ensure_package("src.config", PROJECT_ROOT / "src" / "config")

    capturado = _LoggerCapturado()
    env_module = types.SimpleNamespace(
        ENVIRONMENT="test",
        GOOGLE_MAPS_API_URL="https://maps.local/geocode/json",
        GOOGLE_MAPS_API_KEY=CHAVE,
        COR_ALERT_WRITE_DEADLINE_SECONDS=30.0,
    )
    monkeypatch.setitem(sys.modules, "src.config.env", env_module)
    monkeypatch.setitem(
        sys.modules, "src.config", types.SimpleNamespace(env=env_module)
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.bigquery",
        types.SimpleNamespace(
            save_cor_alert_in_bq_background=lambda **_k: asyncio.sleep(0),
            save_cor_alert_to_queue_background=lambda **_k: asyncio.sleep(0),
            get_datetime=lambda: "2026-11-05T10:00:00.000000",
        ),
    )
    monkeypatch.setitem(
        sys.modules, "src.utils.log", types.SimpleNamespace(logger=capturado)
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.error_interceptor",
        types.SimpleNamespace(interceptor=_passthrough_interceptor),
    )

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def get(self, url, params=None):
            return responder(url, params or {})

    monkeypatch.setitem(
        sys.modules,
        "src.utils.http_client",
        types.SimpleNamespace(InterceptedHTTPClient=FakeClient),
    )

    spec = importlib.util.spec_from_file_location(
        alias, PROJECT_ROOT / "src" / "tools" / "cor_alert_tools.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, capturado


def _sem_endereco_nem_coordenada(capturado: _LoggerCapturado) -> None:
    texto = capturado.texto()
    assert capturado.registros, "a tool deveria ter logado algo"
    for trecho in TRECHOS_PROIBIDOS:
        assert trecho not in texto, f"{trecho!r} vazou para o log:\n{texto}"


async def _criar_alerta(module) -> dict:
    return await module.create_cor_alert(
        "usuario-sintetico-340", "alagamento", "alta", "agua subindo", ENDERECO
    )


@pytest.mark.asyncio
async def test_sucesso_loga_so_metadado(monkeypatch):
    module, capturado = _carregar(
        monkeypatch,
        "cor_dev340_sucesso",
        lambda _url, _params: _resposta(_geocode_ok(com_bairro=True)),
    )

    resultado = await _criar_alerta(module)

    assert resultado["success"] is True
    _sem_endereco_nem_coordenada(capturado)
    texto = capturado.texto()
    assert str(resumir_texto_livre(ENDERECO)) in texto
    assert "geolocalizado: sim" in texto
    assert f"bairro={BAIRRO_NORMALIZADO}" in texto


@pytest.mark.asyncio
async def test_sucesso_via_reverse_geocoding_loga_bairro_normalizado(monkeypatch):
    def responder(_url, params):
        if "latlng" in params:
            return _resposta(REVERSE_OK)
        return _resposta(_geocode_ok(com_bairro=False))

    module, capturado = _carregar(monkeypatch, "cor_dev340_reverse", responder)

    resultado = await _criar_alerta(module)

    assert resultado["success"] is True
    _sem_endereco_nem_coordenada(capturado)
    texto = capturado.texto()
    assert BAIRRO not in texto
    assert f"bairro={BAIRRO_NORMALIZADO}" in texto


@pytest.mark.asyncio
async def test_sem_bairro_loga_nao_identificado(monkeypatch):
    def responder(_url, params):
        if "latlng" in params:
            return _resposta({"status": "ZERO_RESULTS", "results": []})
        return _resposta(_geocode_ok(com_bairro=False))

    module, capturado = _carregar(monkeypatch, "cor_dev340_sem_bairro", responder)

    resultado = await _criar_alerta(module)

    assert resultado["success"] is True
    _sem_endereco_nem_coordenada(capturado)
    texto = capturado.texto()
    assert "geolocalizado: sim" in texto
    assert "bairro=nao_identificado" in texto


@pytest.mark.asyncio
async def test_falha_sem_resultado_loga_so_metadado(monkeypatch):
    module, capturado = _carregar(
        monkeypatch,
        "cor_dev340_falha",
        lambda _url, _params: _resposta({"status": "ZERO_RESULTS", "results": []}),
    )

    resultado = await _criar_alerta(module)

    assert resultado["success"] is True
    _sem_endereco_nem_coordenada(capturado)
    avisos = [r for r in capturado.registros if r[0] == "warning"]
    assert avisos
    assert "geolocalizado: não" in avisos[-1][1]
    assert str(resumir_texto_livre(ENDERECO)) in avisos[-1][1]


@pytest.mark.asyncio
async def test_erro_http_no_geocode_nao_loga_a_url(monkeypatch):
    module, capturado = _carregar(
        monkeypatch,
        "cor_dev340_erro_geocode",
        _resposta_com_erro_http,
    )

    resultado = await _criar_alerta(module)

    assert resultado["success"] is True
    _sem_endereco_nem_coordenada(capturado)
    assert "HTTPStatusError" in capturado.texto()


@pytest.mark.asyncio
async def test_erro_http_no_reverse_geocoding_nao_loga_a_coordenada(monkeypatch):
    def responder(url, params):
        if "latlng" in params:
            return _resposta_com_erro_http(url, params)
        return _resposta(_geocode_ok(com_bairro=False))

    module, capturado = _carregar(monkeypatch, "cor_dev340_erro_reverse", responder)

    resultado = await _criar_alerta(module)

    assert resultado["success"] is True
    _sem_endereco_nem_coordenada(capturado)
    texto = capturado.texto()
    assert "HTTPStatusError" in texto
    assert "bairro=nao_identificado" in texto
