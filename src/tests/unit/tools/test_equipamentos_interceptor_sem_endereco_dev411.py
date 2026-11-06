"""
Testes da DEV-411: a falha na busca de equipamentos por endereço não manda o
endereço digitado pelo cidadão ao error interceptor.

Antes, o `@interceptor` de `get_pluscode_coords_equipments` copiava o argumento
`address` cru no `input_body`, e a mensagem do `GeocodingError` sem plus8
embutia o endereço (que ia no `error_message` e no traceback do report). E o
`InterceptedHTTPClient` do geocode do Google Maps reportava os `params` da
consulta (o endereço) no corpo da falha de conexão ou do timeout. A redação por
padrão tira CPF, telefone e e-mail, não endereço.

Invariantes cobertos aqui, sempre com o envio real ao interceptor e conferindo
todos os reports que saem:
- `address` vira `resumir_texto_livre` (tamanho e tipos de dado pessoal), por
  nome e por posição, nos desfechos que levantam: sem coordenadas, sem plus8 e
  erro inesperado na geocodificação;
- a mensagem do `GeocodingError` não leva o endereço;
- pelo cliente HTTP real do geocode (conexão e timeout), o report do cliente
  leva o endereço resumido e o do decorator também; a requisição de saída
  continua com o endereço;
- o fluxo não muda: o `GeocodingError` continua subindo, a falha do BigQuery
  continua virando o dict de erro e o sucesso devolve o mesmo dict de antes.
"""

import json
from urllib.parse import parse_qs

import httpx
import pytest

from src.tools.equipments import pluscode_service
from src.tools.equipments import utils as equipments_utils
from src.utils import error_interceptor
from src.utils.bigquery import BigQueryQueryError
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
PLUS8 = "589R2PXQ+"
TRECHOS_PROIBIDOS = (
    "Quimera",
    "Inventada",
    "9182",
    "fundos",
)
ERRO_DO_GOOGLE = "google maps fora do ar"
GOOGLE_HOST = "maps.googleapis.com"
NOMINATIM_HOST = "nominatim.openstreetmap.org"
ENDERECO_CONSULTADO = f"{ENDERECO} - Rio de Janeiro, RJ"


class _CapturingTransport(httpx.AsyncBaseTransport):
    """Responde ao error interceptor e guarda os envios."""

    def __init__(self):
        self.requests = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return httpx.Response(200, json={"status": "ok"})


class _GoogleTransport(httpx.BaseTransport):
    """Transporte síncrono dos geocoders (Google Maps e Nominatim; o
    `InterceptedHTTPClient` de equipamentos é `sync=True`): guarda as consultas
    e responde, na ordem, o que estiver em `respostas`; uma exceção é
    levantada, um `httpx.Response` vai como está, o resto vira JSON."""

    def __init__(self):
        self.consultas = []
        self.respostas = []

    def handle_request(self, request):
        assert request.url.host in (GOOGLE_HOST, NOMINATIM_HOST)
        self.consultas.append(request)
        resposta = self.respostas.pop(0)
        if isinstance(resposta, Exception):
            raise resposta
        if isinstance(resposta, httpx.Response):
            return resposta
        return httpx.Response(200, json=resposta)


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

    # Subclasse, e não função, porque o cliente HTTP confere o tipo com
    # `isinstance`.
    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClientCapturado)
    return capturing


@pytest.fixture
def google(monkeypatch):
    """Consultas síncronas ao Google Maps capturadas em memória."""
    google_transport = _GoogleTransport()

    class _ClientCapturado(httpx.Client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = google_transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", _ClientCapturado)
    return google_transport


def _geocode_ok() -> dict:
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


def _geocoder(resultado):
    """`get_plus8_coords_from_address` trocado: devolve o par ou levanta."""

    def geocoder(address):
        assert address == ENDERECO
        if isinstance(resultado, Exception):
            raise resultado
        return resultado

    return geocoder


def _payloads_enviados(transport: _CapturingTransport) -> list:
    payloads = [json.loads(r.content.decode("utf-8")) for r in transport.requests]
    for payload in payloads:
        _sem_endereco(payload)
    return payloads


def _sem_endereco(payload: dict) -> None:
    corpo = json.dumps(payload, ensure_ascii=False)
    for trecho in TRECHOS_PROIBIDOS:
        assert trecho not in corpo, f"dado do endereço no report: {trecho!r}"


def _assert_report_do_decorator(payload: dict, categorias: list | None) -> None:
    assert "tool=equipments" in payload["flowname"]
    assert "function=get_pluscode_coords_equipments" in payload["flowname"]
    esperado = {"address": resumir_texto_livre(ENDERECO)}
    if categorias is not None:
        esperado["categories"] = categorias
    assert json.loads(payload["input_body"]) == esperado


async def _chamar(por_nome: bool, categorias: list | None):
    if por_nome:
        if categorias is None:
            return await pluscode_service.get_pluscode_coords_equipments(
                address=ENDERECO
            )
        return await pluscode_service.get_pluscode_coords_equipments(
            address=ENDERECO, categories=categorias
        )
    if categorias is None:
        return await pluscode_service.get_pluscode_coords_equipments(ENDERECO)
    return await pluscode_service.get_pluscode_coords_equipments(ENDERECO, categorias)


# ---------------------------------------------------------------------------
# Desfechos que levantam e chegam ao decorator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("por_nome", [True, False], ids=["por_nome", "por_posicao"])
@pytest.mark.parametrize("categorias", [None, ["CF"]], ids=["sem_cat", "com_cat"])
async def test_sem_coordenadas_nao_envia_endereco(
    transport, monkeypatch, por_nome, categorias
):
    monkeypatch.setattr(
        pluscode_service, "get_plus8_coords_from_address", _geocoder((None, None))
    )

    with pytest.raises(pluscode_service.GeocodingError, match="No coords found"):
        await _chamar(por_nome, categorias)

    (do_decorator,) = _payloads_enviados(transport)
    _assert_report_do_decorator(do_decorator, categorias)
    assert do_decorator["api_endpoint"] == "internal://GeocodingError"


@pytest.mark.asyncio
@pytest.mark.parametrize("plus8_vazio", [None, ""], ids=["none", "string-vazia"])
@pytest.mark.parametrize("por_nome", [True, False], ids=["por_nome", "por_posicao"])
async def test_sem_plus8_nao_envia_endereco_nem_na_mensagem(
    transport, monkeypatch, plus8_vazio, por_nome
):
    coords = {"lat": LATITUDE, "lng": LONGITUDE, "address": ENDERECO_FORMATADO}
    monkeypatch.setattr(
        pluscode_service,
        "get_plus8_coords_from_address",
        _geocoder((plus8_vazio, coords)),
    )

    with pytest.raises(pluscode_service.GeocodingError) as exc:
        await _chamar(por_nome, None)

    # A mensagem também vai ao agente: sem o endereço.
    mensagem = str(exc.value)
    assert mensagem == "No plus8 code for the geocoded address"
    for trecho in TRECHOS_PROIBIDOS:
        assert trecho not in mensagem

    (do_decorator,) = _payloads_enviados(transport)
    _assert_report_do_decorator(do_decorator, None)
    erro = json.loads(do_decorator["error_response"])
    assert erro["error_message"] == "No plus8 code for the geocoded address"
    assert "GeocodingError" in erro["traceback"]


@pytest.mark.asyncio
async def test_erro_inesperado_na_geocodificacao_nao_envia_endereco(
    transport, monkeypatch
):
    monkeypatch.setattr(
        pluscode_service,
        "get_plus8_coords_from_address",
        _geocoder(KeyError("status")),
    )

    with pytest.raises(KeyError):
        await pluscode_service.get_pluscode_coords_equipments(address=ENDERECO)

    (do_decorator,) = _payloads_enviados(transport)
    _assert_report_do_decorator(do_decorator, None)
    assert do_decorator["api_endpoint"] == "internal://KeyError"


# ---------------------------------------------------------------------------
# Caminho real da geocodificação (InterceptedHTTPClient do Google Maps)
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
async def test_google_fora_nao_envia_endereco(transport, google, falha, status):
    google.respostas = [falha]

    with pytest.raises(type(falha)):
        await pluscode_service.get_pluscode_coords_equipments(address=ENDERECO)

    # A consulta ao Google continua levando o endereço: só o report muda.
    assert len(google.consultas) == 1
    query = parse_qs(google.consultas[0].url.query.decode("utf-8"))
    assert query["address"] == [ENDERECO_CONSULTADO]

    do_cliente, do_decorator = _payloads_enviados(transport)
    assert "function=get_coords_from_google_maps_api" in do_cliente["flowname"]
    assert do_cliente["api_endpoint"] == equipments_utils.env.GOOGLE_MAPS_API_URL
    assert do_cliente["http_status_code"] == status
    corpo = json.loads(do_cliente["input_body"])
    assert corpo["address"] == resumir_texto_livre(ENDERECO_CONSULTADO)
    assert equipments_utils.env.GOOGLE_MAPS_API_KEY not in json.dumps(do_cliente)

    _assert_report_do_decorator(do_decorator, None)


@pytest.mark.asyncio
async def test_google_sem_resultado_nao_envia_endereco(transport, google):
    google.respostas = [{"status": "ZERO_RESULTS", "results": []}]

    with pytest.raises(pluscode_service.GeocodingError, match="No coords found"):
        await pluscode_service.get_pluscode_coords_equipments(ENDERECO)

    assert len(google.consultas) == 1
    (do_decorator,) = _payloads_enviados(transport)
    _assert_report_do_decorator(do_decorator, None)


# ---------------------------------------------------------------------------
# Nominatim (hoje sem uso em `get_plus8_coords_from_address`)
# ---------------------------------------------------------------------------


def test_nominatim_fora_nao_envia_endereco(transport, google):
    """Fora de event loop, como no executor de `_geocodificar`: o cliente
    reporta com `asyncio.run` antes de relançar."""
    google.respostas = [httpx.ConnectError(ERRO_DO_GOOGLE)]

    with pytest.raises(httpx.ConnectError):
        equipments_utils.get_coords_from_nominatim_api(ENDERECO)

    query = parse_qs(google.consultas[0].url.query.decode("utf-8"))
    assert query["q"] == [ENDERECO]

    (do_cliente,) = _payloads_enviados(transport)
    assert "function=get_coords_from_nominatim_api" in do_cliente["flowname"]
    corpo = json.loads(do_cliente["input_body"])
    assert corpo["q"] == resumir_texto_livre(ENDERECO)


def test_nominatim_com_erro_http_nao_poe_a_url_na_mensagem(google):
    """A mensagem do `raise_for_status` traz a URL com o endereço na query; ela
    chegaria ao interceptor pelo `@interceptor` de quem chama."""
    google.respostas = [httpx.Response(503, text="indisponível")]

    with pytest.raises(httpx.HTTPStatusError) as exc:
        equipments_utils.get_coords_from_nominatim_api(ENDERECO)

    assert exc.value.response.status_code == 503
    mensagem = str(exc.value)
    assert mensagem == "Nominatim respondeu HTTP 503"
    for trecho in TRECHOS_PROIBIDOS:
        assert trecho not in mensagem


# ---------------------------------------------------------------------------
# O fluxo que não levanta continua igual (e não reporta pelo decorator)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sucesso_devolve_o_mesmo_dict(transport, google, monkeypatch):
    google.respostas = [_geocode_ok()]
    dados = [{"categoria": "CF", "nome_oficial": "Clínica Sintética"}]

    async def bigquery_ok(**_kwargs):
        return dados

    monkeypatch.setattr(pluscode_service, "get_bigquery_result", bigquery_ok)

    resultado = await pluscode_service.get_pluscode_coords_equipments(
        address=ENDERECO, categories=["CF"]
    )

    assert resultado["inputs"] == {"address": ENDERECO, "categories": ["CF"]}
    assert resultado["data"] == dados
    assert resultado["coords"]["lat"] == LATITUDE
    assert resultado["plus8"]
    assert transport.requests == []


@pytest.mark.asyncio
async def test_falha_do_bigquery_continua_virando_dict(transport, monkeypatch):
    coords = {"lat": LATITUDE, "lng": LONGITUDE, "address": ENDERECO_FORMATADO}
    monkeypatch.setattr(
        pluscode_service,
        "get_plus8_coords_from_address",
        _geocoder((PLUS8, coords)),
    )

    async def bigquery_com_bug(**_kwargs):
        raise BigQueryQueryError("falha sintética")

    monkeypatch.setattr(pluscode_service, "get_bigquery_result", bigquery_com_bug)
    monkeypatch.setattr(
        pluscode_service,
        "logger",
        type(
            "_Log",
            (),
            {"exception": lambda *_a, **_k: None, "error": lambda *_a, **_k: None},
        )(),
    )

    resultado = await pluscode_service.get_pluscode_coords_equipments(ENDERECO)

    assert resultado["error"] == "Erro inesperado na consulta ao BigQuery"
    assert transport.requests == []
