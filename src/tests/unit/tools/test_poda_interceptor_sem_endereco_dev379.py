"""
Testes da DEV-379: a falha em `get_endereco_info` e `get_ipp_street_code` (poda
de árvore, `api_service.py`) não manda nome de rua, bairro nem coordenada ao
error interceptor: nem no `input_body` do `@interceptor`, nem no `api_endpoint`
do report do `InterceptedHTTPClient` do geocode do IPP.

Antes, o decorator copiava os argumentos `str` e `float` das duas funções: o
logradouro do Google e o do IPP, o bairro do IPP e o do Google, a latitude e a
longitude iam ao error interceptor externo, só com a redação por padrão (que
tira CPF e telefone, não endereço). E a falha de conexão ou o timeout do
geocode do IPP mandava a URL inteira, com `Address=<logradouro>, <bairro>` na
query, no `api_endpoint` do report do cliente HTTP.

Invariantes cobertos aqui:
- em `get_ipp_street_code`, logradouros e bairros viram `resumir_texto_livre`
  (tamanho e tipos de dado pessoal) e latitude e longitude vão só com o tipo,
  por nome e por posição;
- o report do cliente HTTP real (falha de conexão e timeout do geocode) vai
  com o endpoint sem a query; a requisição de saída continua com ela;
- vale pelo caminho real: `get_endereco_info` chama `get_ipp_street_code`, a
  consulta ao geocode do IPP falha e saem os dois reports, o do cliente HTTP e
  o do decorator da interna;
- em `get_endereco_info`, os quatro argumentos viram só metadado, também com a
  coordenada em texto;
- o fluxo não muda: a exceção continua subindo e `get_endereco_info` continua
  devolvendo a abertura manual.
"""

import json

import httpx
import pytest

from src.tools.multi_step_service.workflows.poda_de_arvore.api import api_service
from src.utils import error_interceptor
from src.utils.pii import resumir_texto_livre

# Capturado no import, antes do fixture autouse `block_real_error_interceptor`
# (src/tests/unit/conftest.py) trocar o atributo por um mock: os testes precisam
# do payload real que sairia do processo.
_real_send_error_to_interceptor = error_interceptor.send_error_to_interceptor

# Dados sintéticos.
LOGRADOURO_GOOGLE = "Rua Quimera Sintetica 4321"
LOGRADOURO_IPP = "77 - Travessa Zirconio Inventado"
BAIRRO_IPP = "Vila Brumal Ficticia"
BAIRRO_GOOGLE = "Jardim Ermelinda Quarzo"
LATITUDE = -22.9135791
LONGITUDE = -43.2468024
TRECHOS_PROIBIDOS = (
    "Quimera",
    "Sintetica",
    "4321",
    "Zirconio",
    "Inventado",
    "Brumal",
    "Ficticia",
    "Ermelinda",
    "Quarzo",
    "22.91",
    "43.24",
    "9135791",
    "2468024",
)
ERRO_DO_IPP = "geocode do IPP fora do ar"
GEOCODE_IPP_HOST = "pgeo3.rio.rj.gov.br"


class _CapturingTransport(httpx.AsyncBaseTransport):
    """Responde ao error interceptor e guarda os envios. A consulta ao geocode
    do IPP passa pelo `InterceptedHTTPClient` real e falha com `falha_do_ipp`."""

    def __init__(self):
        self.requests = []
        self.consultas_ao_ipp = []
        self.falha_do_ipp = httpx.ConnectError(ERRO_DO_IPP)

    async def handle_async_request(self, request):
        if request.url.host == GEOCODE_IPP_HOST:
            self.consultas_ao_ipp.append(request)
            raise self.falha_do_ipp
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
    # `InterceptedHTTPClient` do geocode: os dois passam pelo transporte acima.
    # Subclasse, e não função, porque o cliente HTTP confere o tipo com
    # `isinstance`.
    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClientCapturado)
    return capturing


def _payloads_enviados(transport: _CapturingTransport) -> list:
    return [json.loads(r.content.decode("utf-8")) for r in transport.requests]


def _payload_enviado(transport: _CapturingTransport) -> dict:
    payloads = _payloads_enviados(transport)
    assert len(payloads) == 1
    return payloads[0]


def _reports_da_falha_do_ipp(transport: _CapturingTransport) -> tuple:
    """Os dois reports da falha do geocode: o do `InterceptedHTTPClient` e o do
    `@interceptor` de `get_ipp_street_code`, nessa ordem."""
    payloads = _payloads_enviados(transport)
    assert len(payloads) == 2
    do_cliente, do_decorator = payloads
    assert do_cliente["api_endpoint"] == api_service._GEOCODE_IPP_URL
    assert "get_ipp_street_code" in do_decorator["flowname"]
    for payload in payloads:
        _sem_endereco(payload)
    return do_cliente, do_decorator


def _sem_endereco(payload: dict) -> None:
    corpo = json.dumps(payload, ensure_ascii=False)
    for trecho in TRECHOS_PROIBIDOS:
        assert trecho not in corpo, f"dado do endereço no report: {trecho!r}"


def _assert_input_body_do_ipp(input_body: dict, bairro_google) -> None:
    assert input_body == {
        "logradouro_nome": resumir_texto_livre(LOGRADOURO_GOOGLE),
        "logradouro_nome_ipp": resumir_texto_livre(LOGRADOURO_IPP),
        "bairro_nome_ipp": resumir_texto_livre(BAIRRO_IPP),
        "latitude": {"tipo": "float"},
        "longitude": {"tipo": "float"},
        **(
            {"bairro_google": resumir_texto_livre(bairro_google)}
            if bairro_google is not None
            else {}
        ),
    }


# ---------------------------------------------------------------------------
# get_ipp_street_code
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "falha, status",
    [
        (httpx.ConnectError(ERRO_DO_IPP), 0),
        (httpx.ReadTimeout(ERRO_DO_IPP), 408),
    ],
    ids=["conexao", "timeout"],
)
async def test_get_ipp_street_code_por_nome_nao_envia_endereco(
    transport, falha, status
):
    transport.falha_do_ipp = falha

    with pytest.raises(type(falha), match=ERRO_DO_IPP):
        await api_service.AddressAPIService().get_ipp_street_code(
            logradouro_nome=LOGRADOURO_GOOGLE,
            logradouro_nome_ipp=LOGRADOURO_IPP,
            bairro_nome_ipp=BAIRRO_IPP,
            latitude=LATITUDE,
            longitude=LONGITUDE,
            bairro_google=BAIRRO_GOOGLE,
        )

    # A consulta ao IPP continua levando o logradouro: só o report muda.
    assert len(transport.consultas_ao_ipp) == 1
    assert "Quimera" in str(transport.consultas_ao_ipp[0].url)

    do_cliente, do_decorator = _reports_da_falha_do_ipp(transport)
    assert do_cliente["http_status_code"] == status
    assert "function=get_ipp_street_code" in do_cliente["flowname"]
    _assert_input_body_do_ipp(json.loads(do_decorator["input_body"]), BAIRRO_GOOGLE)
    assert ERRO_DO_IPP in do_decorator["error_response"]


@pytest.mark.asyncio
async def test_get_ipp_street_code_por_posicao_nao_envia_endereco(transport):
    with pytest.raises(httpx.ConnectError):
        await api_service.AddressAPIService().get_ipp_street_code(
            LOGRADOURO_GOOGLE, LOGRADOURO_IPP, BAIRRO_IPP, LATITUDE, LONGITUDE
        )

    _, do_decorator = _reports_da_falha_do_ipp(transport)
    _assert_input_body_do_ipp(json.loads(do_decorator["input_body"]), None)


# ---------------------------------------------------------------------------
# get_endereco_info
# ---------------------------------------------------------------------------


def _localizacao_sintetica(_latitude, _longitude):
    return api_service.NearestLocation(
        id_logradouro=123,
        name_logradouro=LOGRADOURO_IPP,
        id_bairro=77,
        name_bairro=BAIRRO_IPP,
    )


@pytest.mark.asyncio
async def test_get_endereco_info_com_ipp_fora_do_ar_nao_envia_endereco(
    transport, monkeypatch
):
    """Caminho de produção (`workflow.py` chama `get_endereco_info` por nome):
    a falha do geocode do IPP é reportada pelo cliente HTTP e pelo decorator
    de `get_ipp_street_code`, e `get_endereco_info` devolve a abertura manual."""
    servico = api_service.AddressAPIService()
    monkeypatch.setattr(
        servico, "get_nearest_logradouro_and_bairro", _localizacao_sintetica
    )

    resultado = await servico.get_endereco_info(
        latitude=LATITUDE,
        longitude=LONGITUDE,
        logradouro_google=LOGRADOURO_GOOGLE,
        bairro_google=BAIRRO_GOOGLE,
    )

    assert resultado == {"error": ERRO_DO_IPP, "abertura_manual": True}
    assert len(transport.consultas_ao_ipp) == 1
    _, do_decorator = _reports_da_falha_do_ipp(transport)
    _assert_input_body_do_ipp(json.loads(do_decorator["input_body"]), BAIRRO_GOOGLE)


class _LogQueFalha:
    """Nenhum caminho de hoje deixa uma exceção escapar de `get_endereco_info`
    (os dois blocos capturam `Exception`); a falha no próprio log do `except`
    é o que faz uma escapar e o decorator dela reportar."""

    def __getattr__(self, _nivel):
        def _falha(*_args, **_kwargs):
            raise RuntimeError("log indisponível")

        return _falha


@pytest.mark.asyncio
async def test_get_endereco_info_por_nome_nao_envia_endereco(transport, monkeypatch):
    servico = api_service.AddressAPIService()
    monkeypatch.setattr(api_service, "logger", _LogQueFalha())

    with pytest.raises(RuntimeError, match="log indisponível"):
        await servico.get_endereco_info(
            latitude=LATITUDE,
            longitude=LONGITUDE,
            logradouro_google=LOGRADOURO_GOOGLE,
            bairro_google=BAIRRO_GOOGLE,
        )

    payload = _payload_enviado(transport)
    assert "get_endereco_info" in payload["flowname"]
    assert json.loads(payload["input_body"]) == {
        "latitude": {"tipo": "float"},
        "longitude": {"tipo": "float"},
        "logradouro_google": resumir_texto_livre(LOGRADOURO_GOOGLE),
        "bairro_google": resumir_texto_livre(BAIRRO_GOOGLE),
    }
    _sem_endereco(payload)


@pytest.mark.asyncio
async def test_get_endereco_info_por_posicao_com_coordenada_em_texto(
    transport, monkeypatch
):
    servico = api_service.AddressAPIService()
    monkeypatch.setattr(api_service, "logger", _LogQueFalha())

    with pytest.raises(RuntimeError):
        await servico.get_endereco_info(
            str(LATITUDE), str(LONGITUDE), LOGRADOURO_GOOGLE
        )

    payload = _payload_enviado(transport)
    assert json.loads(payload["input_body"]) == {
        "latitude": resumir_texto_livre(str(LATITUDE)),
        "longitude": resumir_texto_livre(str(LONGITUDE)),
        "logradouro_google": resumir_texto_livre(LOGRADOURO_GOOGLE),
    }
    _sem_endereco(payload)
