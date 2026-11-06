"""
Testes da DEV-387: a falha na geolocalização de um endereço digitado pelo
cidadão não manda o endereço nem a coordenada ao error interceptor.

Antes, o `@interceptor` de `google_geolocator` (poda de árvore) e de
`get_coordinates_google` e `geocode_address` (alerta do COR) copiava o
argumento `address` cru no `input_body`. E o `InterceptedHTTPClient` do
geocode do Google Maps reportava os `params` da consulta (o endereço) no corpo
da falha de conexão ou do timeout; o do reverse geocode, a coordenada
(`latlng`). A redação por padrão tira CPF, telefone e e-mail, não endereço.

Invariantes cobertos aqui, sempre com o envio real ao interceptor e conferindo
todos os reports que saem:
- nas três funções, `address` vira `resumir_texto_livre` (tamanho e tipos de
  dado pessoal), por nome e por posição;
- o report do cliente HTTP real do geocode (conexão e timeout) leva o endereço
  resumido e o do reverse geocode leva a coordenada resumida; a requisição de
  saída continua com os dois;
- em `google_geolocator`, pelo cliente real do `async_googlemaps`, a falha do
  aiohttp cuja mensagem traz a URL da consulta (endereço, coordenada e chave
  na query) chega ao interceptor só com o tipo do erro, sem a URL na mensagem
  nem no traceback; o status e o texto de um erro da API do Google seguem;
- o fluxo não muda: a exceção continua subindo (no `google_geolocator`, com o
  mesmo tipo), e a falha do Google continua virando `{}` em
  `get_coordinates_google`.
"""

import json
import types
from urllib.parse import parse_qs

import aiohttp
import httpx
import pytest
from aiohttp.client_reqrep import RequestInfo
from async_googlemaps import exceptions as gmaps_exceptions
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from src.tools import cor_alert_tools
from src.tools.multi_step_service.workflows.poda_de_arvore.api import api_service
from src.utils import error_interceptor
from src.utils.pii import resumir_texto_livre

# Capturado no import, antes do fixture autouse `block_real_error_interceptor`
# (src/tests/unit/conftest.py) trocar o atributo por um mock: os testes precisam
# do payload real que sairia do processo.
_real_send_error_to_interceptor = error_interceptor.send_error_to_interceptor

# Dados sintéticos.
ENDERECO = "Rua Quimera Inventada 9182 casa dos fundos"
ENDERECO_FORMATADO = "R. Quimera Inventada, 9182 - Rio de Janeiro - RJ"
LATITUDE = -22.9471358
LONGITUDE = -43.3684219
TRECHOS_PROIBIDOS = (
    "Quimera",
    "Inventada",
    "9182",
    "fundos",
    "22.94",
    "43.36",
    "9471358",
    "3684219",
)
ERRO_DO_GOOGLE = "google maps fora do ar"
GOOGLE_HOST = "maps.googleapis.com"


class _CapturingTransport(httpx.AsyncBaseTransport):
    """Responde ao error interceptor e guarda os envios. As consultas ao Google
    Maps passam pelo `InterceptedHTTPClient` real e recebem, na ordem, o que
    estiver em `respostas_do_google`: uma exceção é levantada, o resto vira
    corpo JSON."""

    def __init__(self):
        self.requests = []
        self.consultas_ao_google = []
        self.respostas_do_google = []

    async def handle_async_request(self, request):
        if request.url.host == GOOGLE_HOST:
            self.consultas_ao_google.append(request)
            resposta = self.respostas_do_google.pop(0)
            if isinstance(resposta, Exception):
                raise resposta
            return httpx.Response(200, json=resposta)
        self.requests.append(request)
        return httpx.Response(200, json={"status": "ok"})


@pytest.fixture
def transport(monkeypatch):
    """Envio real ao interceptor, com o HTTP capturado em memória."""
    capturing = _CapturingTransport()
    monkeypatch.setattr(
        error_interceptor.env,
        "ERROR_INTERCEPTOR_URL",
        "https://test.interceptor.local/api",
    )
    monkeypatch.setattr(error_interceptor.env, "ERROR_INTERCEPTOR_TOKEN", "test-token")
    monkeypatch.setattr(
        error_interceptor,
        "send_error_to_interceptor",
        _real_send_error_to_interceptor,
    )

    class _AsyncClientCapturado(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = capturing
            super().__init__(*args, **kwargs)

    # `httpx.AsyncClient` é o mesmo para o error interceptor e para o
    # `InterceptedHTTPClient` do Google Maps: os dois passam pelo transporte
    # acima. Subclasse, e não função, porque o cliente HTTP confere o tipo com
    # `isinstance`.
    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClientCapturado)
    return capturing


class _LogQueFalha:
    """As funções do COR capturam a falha do Google e só logam; a falha no
    próprio log é o que faz uma exceção escapar e o decorator reportar."""

    def __getattr__(self, _nivel):
        def _falha(*_args, **_kwargs):
            raise RuntimeError("log indisponível")

        return _falha


def _geocode_sem_bairro() -> dict:
    """Resposta do geocode do Google com coordenada e sem bairro: leva
    `geocode_address` ao reverse geocode."""
    return {
        "status": "OK",
        "results": [
            {
                "formatted_address": ENDERECO_FORMATADO,
                "geometry": {"location": {"lat": LATITUDE, "lng": LONGITUDE}},
                "address_components": [],
            }
        ],
    }


def _payloads_enviados(transport: _CapturingTransport) -> list:
    payloads = [json.loads(r.content.decode("utf-8")) for r in transport.requests]
    for payload in payloads:
        _sem_endereco(payload)
    return payloads


def _sem_endereco(payload: dict) -> None:
    corpo = json.dumps(payload, ensure_ascii=False)
    for trecho in TRECHOS_PROIBIDOS:
        assert trecho not in corpo, f"dado do endereço no report: {trecho!r}"


def _query_da_consulta(request: httpx.Request) -> dict:
    return parse_qs(request.url.query.decode("utf-8"))


def _assert_report_do_cliente(payload: dict, funcao: str, status: int) -> dict:
    """Report do `InterceptedHTTPClient`: o endpoint sem query e o corpo com o
    texto livre resumido. Devolve o corpo."""
    assert payload["api_endpoint"] == cor_alert_tools.GOOGLE_MAPS_API_URL
    assert f"function={funcao}" in payload["flowname"]
    assert payload["http_status_code"] == status
    return json.loads(payload["input_body"])


def _assert_report_do_decorator(payload: dict, funcao: str) -> None:
    assert f"function={funcao}" in payload["flowname"]
    assert json.loads(payload["input_body"]) == {
        "address": resumir_texto_livre(ENDERECO)
    }


# ---------------------------------------------------------------------------
# get_coordinates_google (alerta do COR)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "falha, status",
    [
        (httpx.ConnectError(ERRO_DO_GOOGLE), 0),
        (httpx.ReadTimeout(ERRO_DO_GOOGLE), 408),
    ],
    ids=["conexao", "timeout"],
)
async def test_get_coordinates_google_com_google_fora_nao_envia_endereco(
    transport, falha, status
):
    transport.respostas_do_google = [falha]

    resultado = await cor_alert_tools.get_coordinates_google(ENDERECO)

    assert resultado == {}
    # A consulta ao Google continua levando o endereço: só o report muda.
    assert len(transport.consultas_ao_google) == 1
    query = _query_da_consulta(transport.consultas_ao_google[0])
    assert query["address"] == [f"{ENDERECO}, Rio de Janeiro, RJ"]

    (do_cliente,) = _payloads_enviados(transport)
    corpo = _assert_report_do_cliente(do_cliente, "get_coordinates_google", status)
    assert corpo["address"] == resumir_texto_livre(f"{ENDERECO}, Rio de Janeiro, RJ")
    assert "test-google-key" not in json.dumps(do_cliente)


@pytest.mark.asyncio
@pytest.mark.parametrize("por_nome", [True, False], ids=["por_nome", "por_posicao"])
async def test_get_coordinates_google_excecao_que_escapa_nao_envia_endereco(
    transport, monkeypatch, por_nome
):
    transport.respostas_do_google = [httpx.ConnectError(ERRO_DO_GOOGLE)]
    monkeypatch.setattr(cor_alert_tools, "logger", _LogQueFalha())

    with pytest.raises(RuntimeError, match="log indisponível"):
        if por_nome:
            await cor_alert_tools.get_coordinates_google(address=ENDERECO)
        else:
            await cor_alert_tools.get_coordinates_google(ENDERECO)

    do_cliente, do_decorator = _payloads_enviados(transport)
    _assert_report_do_cliente(do_cliente, "get_coordinates_google", 0)
    _assert_report_do_decorator(do_decorator, "get_coordinates_google")


# ---------------------------------------------------------------------------
# geocode_address (alerta do COR)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("por_nome", [True, False], ids=["por_nome", "por_posicao"])
async def test_geocode_address_excecao_que_escapa_nao_envia_endereco(
    transport, monkeypatch, por_nome
):
    """O geocode acha a coordenada sem bairro e o log de antes do reverse
    geocode falha dentro de `geocode_address`: só o decorator dela reporta."""
    transport.respostas_do_google = [_geocode_sem_bairro()]
    monkeypatch.setattr(cor_alert_tools, "logger", _LogQueFalha())

    with pytest.raises(RuntimeError, match="log indisponível"):
        if por_nome:
            await cor_alert_tools.geocode_address(address=ENDERECO)
        else:
            await cor_alert_tools.geocode_address(ENDERECO)

    (do_decorator,) = _payloads_enviados(transport)
    _assert_report_do_decorator(do_decorator, "geocode_address")


@pytest.mark.asyncio
async def test_geocode_address_com_reverse_geocode_fora_nao_envia_coordenada(
    transport,
):
    """Caminho real: o geocode acha a coordenada sem bairro, o reverse geocode
    falha e o report do cliente HTTP dele vai sem a coordenada."""
    transport.respostas_do_google = [
        _geocode_sem_bairro(),
        httpx.ConnectError(ERRO_DO_GOOGLE),
    ]

    resultado = await cor_alert_tools.geocode_address(ENDERECO)

    assert resultado["lat"] == LATITUDE
    assert resultado["bairro_raw"] == ""
    # O reverse geocode continua consultando a coordenada.
    assert len(transport.consultas_ao_google) == 2
    query = _query_da_consulta(transport.consultas_ao_google[1])
    assert query["latlng"] == [f"{LATITUDE},{LONGITUDE}"]

    (do_cliente,) = _payloads_enviados(transport)
    corpo = _assert_report_do_cliente(do_cliente, "reverse_geocode", 0)
    assert corpo["latlng"] == resumir_texto_livre(f"{LATITUDE},{LONGITUDE}")


# ---------------------------------------------------------------------------
# google_geolocator (poda de árvore)
# ---------------------------------------------------------------------------


def _cliente_google_que_falha(falha: Exception):
    """`AsyncClient` do `async_googlemaps` cuja consulta falha como a API do
    Google Maps fora do ar: `google_geolocator` não usa o cliente interceptado,
    então a falha só chega ao interceptor pelo decorator."""

    class _ClienteGoogle:
        def __init__(self, _session, key):
            assert key

        async def geocode(self, address):
            assert address == ENDERECO
            raise falha

    return _ClienteGoogle


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "falha",
    [
        gmaps_exceptions.TransportError(Exception(ERRO_DO_GOOGLE)),
        gmaps_exceptions.ApiError("UNKNOWN_ERROR", ERRO_DO_GOOGLE),
        gmaps_exceptions.Timeout(),
    ],
    ids=["transporte", "api", "timeout"],
)
@pytest.mark.parametrize("por_nome", [True, False], ids=["por_nome", "por_posicao"])
async def test_google_geolocator_com_google_fora_nao_envia_endereco(
    transport, monkeypatch, falha, por_nome
):
    monkeypatch.setattr(api_service, "AsyncClient", _cliente_google_que_falha(falha))
    servico = api_service.AddressAPIService()

    with pytest.raises(type(falha)):
        if por_nome:
            await servico.google_geolocator(address=ENDERECO)
        else:
            await servico.google_geolocator(ENDERECO)

    (do_decorator,) = _payloads_enviados(transport)
    _assert_report_do_decorator(do_decorator, "google_geolocator")
    assert type(falha).__name__ in do_decorator["error_response"]


# Pelo cliente real do `async_googlemaps`: só a sessão do aiohttp é trocada. A
# biblioteca monta a URL com o endereço (ou a coordenada) e a chave na query e
# embrulha a falha do aiohttp em `TransportError(e)`, cuja mensagem é a do erro
# do aiohttp, com a URL inteira.
CHAVE_GOOGLE = "AIzaChaveSinteticaDev387"


def _content_type_error(url: str) -> Exception:
    return aiohttp.ContentTypeError(
        RequestInfo(URL(url), "GET", CIMultiDictProxy(CIMultiDict()), URL(url)),
        (),
        status=200,
        message="Attempt to decode JSON with unexpected mimetype: text/html",
    )


def _too_many_redirects(url: str) -> Exception:
    return aiohttp.TooManyRedirects(
        RequestInfo(URL(url), "GET", CIMultiDictProxy(CIMultiDict()), URL(url)),
        (),
    )


class _RespostaDoGoogle:
    def __init__(self, corpo: dict):
        self.status = 200
        self._corpo = corpo

    async def json(self):
        return self._corpo


@pytest.fixture
def sessao_do_google(monkeypatch):
    """Troca `aiohttp.ClientSession.get`: cada consulta recebe, na ordem, o que
    estiver em `respostas` (uma função da URL que devolve a exceção a levantar,
    ou o corpo JSON da resposta) e a URL fica em `urls`."""
    estado = types.SimpleNamespace(respostas=[], urls=[])

    class _Consulta:
        def __init__(self, url):
            self.url = url

        async def __aenter__(self):
            resposta = estado.respostas.pop(0)
            if callable(resposta):
                raise resposta(self.url)
            return _RespostaDoGoogle(resposta)

        async def __aexit__(self, *_exc):
            return False

    def _get(_sessao, url, **_kwargs):
        estado.urls.append(url)
        return _Consulta(url)

    monkeypatch.setattr(aiohttp.ClientSession, "get", _get)
    monkeypatch.setattr(api_service.env, "GMAPS_API_TOKEN", CHAVE_GOOGLE)
    return estado


def _sem_url_do_google(payload: dict) -> None:
    corpo = json.dumps(payload, ensure_ascii=False)
    assert CHAVE_GOOGLE not in corpo
    assert "address=" not in corpo
    assert "latlng=" not in corpo


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "falha_do_aiohttp",
    [_content_type_error, _too_many_redirects],
    ids=["content_type", "redirects"],
)
async def test_google_geolocator_falha_do_aiohttp_com_url_nao_envia_endereco(
    transport, sessao_do_google, falha_do_aiohttp
):
    sessao_do_google.respostas = [falha_do_aiohttp]

    with pytest.raises(gmaps_exceptions.TransportError) as excinfo:
        await api_service.AddressAPIService().google_geolocator(ENDERECO)

    # A consulta continua levando o endereço, e a falha original o trazia.
    (url,) = sessao_do_google.urls
    assert "Quimera" in url
    assert "Quimera" in str(falha_do_aiohttp(url))
    # Quem chama recebe o mesmo tipo, com o tipo do erro interno na mensagem.
    nome_do_erro = falha_do_aiohttp(url).__class__.__name__
    assert str(excinfo.value) == f"{nome_do_erro} na consulta ao Google Maps"

    (do_decorator,) = _payloads_enviados(transport)
    _assert_report_do_decorator(do_decorator, "google_geolocator")
    _sem_url_do_google(do_decorator)
    assert nome_do_erro in do_decorator["error_response"]


@pytest.mark.asyncio
async def test_google_geolocator_reverse_geocode_com_url_nao_envia_coordenada(
    transport, sessao_do_google
):
    """O geocode acha a coordenada sem endereço formatado e o reverse geocode
    falha com a coordenada na URL."""
    sessao_do_google.respostas = [
        {
            "status": "OK",
            "results": [
                {
                    "geometry": {"location": {"lat": LATITUDE, "lng": LONGITUDE}},
                    "address_components": [],
                }
            ],
        },
        _content_type_error,
    ]

    with pytest.raises(gmaps_exceptions.TransportError):
        await api_service.AddressAPIService().google_geolocator(ENDERECO)

    assert len(sessao_do_google.urls) == 2
    assert "9471358" in sessao_do_google.urls[1]

    (do_decorator,) = _payloads_enviados(transport)
    _assert_report_do_decorator(do_decorator, "google_geolocator")
    _sem_url_do_google(do_decorator)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resposta",
    [
        {"status": "REQUEST_DENIED", "error_message": "chave recusada"},
        {"status": "UNKNOWN_ERROR"},
    ],
    ids=["negada", "desconhecido"],
)
async def test_google_geolocator_erro_da_api_mantem_status_do_google(
    transport, sessao_do_google, resposta
):
    """O `ApiError` que a biblioteca embrulha não leva URL: a mensagem dele (o
    status e o texto do Google) segue para o diagnóstico."""
    sessao_do_google.respostas = [resposta]

    with pytest.raises(gmaps_exceptions.TransportError) as excinfo:
        await api_service.AddressAPIService().google_geolocator(ENDERECO)

    assert resposta["status"] in str(excinfo.value)
    (do_decorator,) = _payloads_enviados(transport)
    _assert_report_do_decorator(do_decorator, "google_geolocator")
    _sem_url_do_google(do_decorator)
    assert resposta["status"] in do_decorator["error_response"]


def test_erro_do_google_sem_url_preserva_http_error_e_erro_vazio():
    http = api_service._erro_do_google_sem_url(gmaps_exceptions.HTTPError(503))
    assert isinstance(http, gmaps_exceptions.HTTPError)
    assert str(http) == "HTTP Error: 503"

    embrulhado = api_service._erro_do_google_sem_url(
        gmaps_exceptions.TransportError(gmaps_exceptions.HTTPError(500))
    )
    assert str(embrulhado) == "HTTP Error: 500"

    vazio = api_service._erro_do_google_sem_url(gmaps_exceptions.TransportError())
    assert str(vazio) == "An unknown error occurred."
