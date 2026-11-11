"""
Testes da DEV-510: a falha das APIs do IPTU (`IPTUAPIService`) não manda
inscrição imobiliária, número da guia, cotas nem exercício ao error interceptor
externo.

Antes, o `InterceptedHTTPClient` de `_make_api_request` (ConsultarGuias,
ConsultarCotas, ConsultarDARM e DownloadPdfDARM) e o de
`get_divida_ativa_info` reportavam o `params`/`data` da requisição em claro no
`input_body`: `redact_body` só mascara credenciais (`token`, `chaveacesso`), e
nenhum desses nomes está lá. E `get_imovel_info` reportava a URL com a
inscrição no caminho no `api_endpoint`.

Invariantes cobertos aqui, pelo caminho real (o envio ao interceptor sai pelo
`send_error_to_interceptor` de verdade, com o HTTP capturado em memória):
- HTTP 5xx na última tentativa, timeout e erro de conexão nos quatro endpoints
  da fachada do IPTU: o `input_body` leva só tamanho e tipo de cada campo;
- erro de conexão e timeout na consulta de dívidas da Dívida Ativa: o mesmo
  para `inscricaoImobiliaria`, e a `ChaveAcesso` da autenticação continua
  redigida;
- `get_imovel_info`: o `api_endpoint` vai sem a inscrição;
- a requisição de saída não muda: a API recebe os parâmetros em claro;
- o fluxo não muda: a exceção de domínio continua subindo.
"""

import json
from urllib.parse import parse_qs

import httpx
import pytest

from src.tools.multi_step_service.workflows.iptu_pagamento.api import api_service
from src.tools.multi_step_service.workflows.iptu_pagamento.api.exceptions import (
    APIUnavailableError,
)
from src.utils import error_interceptor
from src.utils.pii import resumir_texto_livre

# Capturado no import, antes do fixture autouse `block_real_error_interceptor`
# (src/tests/unit/conftest.py) trocar o atributo por um mock: os testes precisam
# do payload real que sairia do processo.
_real_send_error_to_interceptor = error_interceptor.send_error_to_interceptor

# Dados sintéticos.
INSCRICAO = "90817263"
INSCRICAO_DIGITADA = "9081726-3"
EXERCICIO = 2031
NUMERO_GUIA = "86420"
COTAS = ["75319", "97531"]
COTAS_STR = ",".join(COTAS)
TRECHOS_PROIBIDOS = (INSCRICAO, "9081726", NUMERO_GUIA, "75319", "97531")

IPTU_HOST = "iptu.sintetico.local"
DIVIDA_HOST = "divida.sintetico.local"
IMOVEL_HOST = "imovel.sintetico.local"
INTERCEPTOR_HOST = "interceptor.sintetico.local"
CHAVE_DIVIDA = "chave-divida-sintetica"


class _CapturingTransport(httpx.AsyncBaseTransport):
    """Guarda os envios ao interceptor e responde às APIs com `resposta_api`.

    `resposta_api` é um `httpx.Response` (devolvido a toda chamada às APIs) ou
    uma exceção do httpx (levantada); `auth_divida` responde à autenticação da
    Dívida Ativa, para que a falha caia na consulta de dívidas.
    """

    def __init__(self):
        self.ao_interceptor = []
        self.as_apis = []
        self.resposta_api = httpx.Response(503, text="Service Unavailable")

    async def handle_async_request(self, request):
        if request.url.host == INTERCEPTOR_HOST:
            self.ao_interceptor.append(request)
            return httpx.Response(200, json={"status": "ok"})

        self.as_apis.append(request)
        if request.url.path.endswith("/security/token"):
            return httpx.Response(200, json={"access_token": "token-sintetico"})
        if isinstance(self.resposta_api, Exception):
            raise self.resposta_api
        return self.resposta_api


@pytest.fixture
def transport(monkeypatch):
    """Envio real ao interceptor, com o HTTP capturado em memória."""
    capturing = _CapturingTransport()
    monkeypatch.setattr(
        error_interceptor.env,
        "ERROR_INTERCEPTOR_URL",
        f"https://{INTERCEPTOR_HOST}/api",
    )
    monkeypatch.setattr(error_interceptor.env, "ERROR_INTERCEPTOR_TOKEN", "t")
    monkeypatch.setattr(
        error_interceptor,
        "send_error_to_interceptor",
        _real_send_error_to_interceptor,
    )
    monkeypatch.setattr(api_service.env, "IPTU_API_URL", f"https://{IPTU_HOST}/api")
    monkeypatch.setattr(api_service.env, "IPTU_API_TOKEN", "token-iptu-sintetico")
    monkeypatch.setattr(
        api_service.env, "DIVIDA_ATIVA_API_URL", f"https://{DIVIDA_HOST}/api"
    )
    monkeypatch.setattr(api_service.env, "DIVIDA_ATIVA_ACCESS_KEY", CHAVE_DIVIDA)
    monkeypatch.setattr(api_service.env, "WA_IPTU_URL", f"https://{IMOVEL_HOST}/api")
    monkeypatch.setattr(
        api_service, "encrypt_token_rsa", lambda **_kwargs: "token-cifrado"
    )

    async def sem_espera(_segundos):
        return None

    monkeypatch.setattr(api_service.asyncio, "sleep", sem_espera)

    class _AsyncClientCapturado(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            # Com `proxy`, o httpx monta um transporte próprio que tem
            # precedência sobre `transport`: sem tirá-lo, a chamada sairia.
            kwargs.pop("proxy", None)
            kwargs["transport"] = capturing
            super().__init__(*args, **kwargs)

    # `httpx.AsyncClient` é o mesmo para o error interceptor e para o
    # `InterceptedHTTPClient` do IPTU. Subclasse, e não função, porque o
    # cliente HTTP confere o tipo com `isinstance`.
    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClientCapturado)
    return capturing


def _payload_enviado(transport: _CapturingTransport) -> dict:
    assert len(transport.ao_interceptor) == 1
    payload = json.loads(transport.ao_interceptor[0].content.decode("utf-8"))
    _sem_contribuinte(payload)
    return payload


def _sem_contribuinte(payload: dict) -> None:
    corpo = json.dumps(payload, ensure_ascii=False)
    for trecho in TRECHOS_PROIBIDOS:
        assert trecho not in corpo, f"{trecho!r} chegou ao interceptor"


def _input_body(payload: dict):
    """O `input_body` vai ao interceptor serializado em JSON."""
    corpo = payload["input_body"]
    return json.loads(corpo) if isinstance(corpo, str) else corpo


def _query_da_api(transport: _CapturingTransport) -> dict:
    """Parâmetros que a API do IPTU recebeu na última chamada."""
    ultima = transport.as_apis[-1]
    return {k: v[0] for k, v in parse_qs(ultima.url.query.decode()).items()}


def _resumo(valor: str) -> dict:
    return resumir_texto_livre(valor)


CHAMADAS_DA_FACHADA = [
    pytest.param(
        "ConsultarGuias",
        lambda s: s.consultar_guias(INSCRICAO_DIGITADA, EXERCICIO),
        {"inscricao": INSCRICAO, "exercicio": str(EXERCICIO)},
        id="consultar_guias",
    ),
    pytest.param(
        "ConsultarCotas",
        lambda s: s.obter_cotas(INSCRICAO_DIGITADA, EXERCICIO, NUMERO_GUIA),
        {"inscricao": INSCRICAO, "exercicio": str(EXERCICIO), "guia": NUMERO_GUIA},
        id="obter_cotas",
    ),
    pytest.param(
        "ConsultarDARM",
        lambda s: s.consultar_darm(INSCRICAO_DIGITADA, EXERCICIO, NUMERO_GUIA, COTAS),
        {
            "inscricao": INSCRICAO,
            "exercicio": str(EXERCICIO),
            "guia": NUMERO_GUIA,
            "cotas": COTAS_STR,
        },
        id="consultar_darm",
    ),
    pytest.param(
        "DownloadPdfDARM",
        lambda s: s.download_pdf_darm(
            INSCRICAO_DIGITADA, EXERCICIO, NUMERO_GUIA, COTAS
        ),
        {
            "inscricao": INSCRICAO,
            "exercicio": str(EXERCICIO),
            "guia": NUMERO_GUIA,
            "cotas": COTAS_STR,
        },
        id="download_pdf_darm",
    ),
]

FALHAS_DA_API = [
    pytest.param(httpx.Response(503, text="Service Unavailable"), 503, id="http-503"),
    pytest.param(
        httpx.Response(500, text="6000 - Connect required before calling"),
        500,
        id="http-500-reconexao",
    ),
    pytest.param(httpx.Response(504, text="Gateway Timeout"), 504, id="http-504"),
    pytest.param(httpx.ReadTimeout("timeout sintetico"), 408, id="timeout"),
    pytest.param(httpx.ConnectError("conexao recusada"), 0, id="conexao"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("falha,status_reportado", FALHAS_DA_API)
@pytest.mark.parametrize("endpoint,chamada,params_esperados", CHAMADAS_DA_FACHADA)
async def test_falha_da_fachada_do_iptu_nao_envia_contribuinte(
    transport, falha, status_reportado, endpoint, chamada, params_esperados
):
    transport.resposta_api = falha
    service = api_service.IPTUAPIService(user_id="usuario-sintetico")

    with pytest.raises(APIUnavailableError):
        await chamada(service)

    payload = _payload_enviado(transport)
    assert payload["api_endpoint"] == f"https://{IPTU_HOST}/api/{endpoint}"
    assert payload["http_status_code"] == status_reportado
    assert _input_body(payload) == {
        campo: _resumo(valor) for campo, valor in params_esperados.items()
    }

    # A requisição de saída não muda: a API recebe os parâmetros em claro, e o
    # token segue na query string.
    query = _query_da_api(transport)
    assert query == {**params_esperados, "token": "token-iptu-sintetico"}


@pytest.mark.asyncio
async def test_retry_so_reporta_a_ultima_tentativa(transport):
    """As tentativas intermediárias não alertam; a última vai sem contribuinte."""
    transport.resposta_api = httpx.Response(503, text="Service Unavailable")
    service = api_service.IPTUAPIService(user_id="usuario-sintetico")

    with pytest.raises(APIUnavailableError):
        await service.consultar_guias(INSCRICAO, EXERCICIO)

    assert len(transport.as_apis) == api_service.API_MAX_TENTATIVAS
    _payload_enviado(transport)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "falha,status_reportado",
    [
        pytest.param(httpx.ConnectError("conexao recusada"), 0, id="conexao"),
        pytest.param(httpx.ReadTimeout("timeout sintetico"), 408, id="timeout"),
    ],
)
async def test_falha_da_divida_ativa_nao_envia_inscricao(
    transport, falha, status_reportado
):
    transport.resposta_api = falha
    service = api_service.IPTUAPIService(user_id="usuario-sintetico")

    with pytest.raises(APIUnavailableError):
        await service.get_divida_ativa_info(INSCRICAO_DIGITADA)

    payload = _payload_enviado(transport)
    assert payload["api_endpoint"] == (
        f"https://{DIVIDA_HOST}/api/v2/cdas/dividas-contribuinte"
    )
    assert payload["http_status_code"] == status_reportado
    assert _input_body(payload) == {
        "origem_solicitação": 0,
        "inscricaoImobiliaria": _resumo(INSCRICAO),
    }

    # A consulta de saída leva a inscrição em claro.
    consulta = transport.as_apis[-1]
    assert parse_qs(consulta.content.decode())["inscricaoImobiliaria"] == [INSCRICAO]


@pytest.mark.asyncio
async def test_falha_na_autenticacao_da_divida_ativa_segue_sem_chave(transport):
    """A `ChaveAcesso` continua redigida no report da autenticação."""

    erro = httpx.ConnectError("autenticacao fora")

    async def handle(request):
        if request.url.host == INTERCEPTOR_HOST:
            transport.ao_interceptor.append(request)
            return httpx.Response(200, json={"status": "ok"})
        transport.as_apis.append(request)
        raise erro

    transport.handle_async_request = handle
    service = api_service.IPTUAPIService(user_id="usuario-sintetico")

    with pytest.raises(APIUnavailableError):
        await service.get_divida_ativa_info(INSCRICAO)

    payload = _payload_enviado(transport)
    assert payload["api_endpoint"] == f"https://{DIVIDA_HOST}/api/security/token"
    assert CHAVE_DIVIDA not in json.dumps(payload)
    assert _input_body(payload)["ChaveAcesso"] == "<redacted>"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "falha,status_reportado",
    [
        pytest.param(httpx.ConnectError("conexao recusada"), 0, id="conexao"),
        pytest.param(httpx.ReadTimeout("timeout sintetico"), 408, id="timeout"),
    ],
)
async def test_falha_do_imovel_nao_envia_inscricao_no_endpoint(
    transport, falha, status_reportado
):
    transport.resposta_api = falha
    service = api_service.IPTUAPIService(user_id="usuario-sintetico")

    with pytest.raises(APIUnavailableError):
        await service.get_imovel_info(INSCRICAO_DIGITADA)

    payload = _payload_enviado(transport)
    assert payload["api_endpoint"] == f"https://{IMOVEL_HOST}/api/<inscricao>"
    assert payload["http_status_code"] == status_reportado

    # A requisição de saída continua com a inscrição no caminho.
    assert transport.as_apis[-1].url.path == f"/api/{INSCRICAO}"


def test_campos_do_contribuinte_cobrem_os_parametros_das_apis():
    """Os nomes casam sem diferenciar maiúsculas, como no `InterceptedHTTPClient`."""
    campos = {campo.lower() for campo in api_service.CAMPOS_DO_CONTRIBUINTE}
    assert {
        "inscricao",
        "exercicio",
        "guia",
        "cotas",
        "inscricaoimobiliaria",
    } <= campos
