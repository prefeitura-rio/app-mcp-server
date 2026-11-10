"""
Testes da DEV-488: a falha de `internal_request` na chamada à PGM não manda a
credencial da PGM nem os dados fiscais do contribuinte ao error interceptor.

Antes, o `@interceptor` de `internal_request` copiava `request_kwargs` inteiro
no `input_body`: a `ChaveAcesso` da PGM (obtenção do token), o token Bearer
emitido pela PGM e o `data` da consulta (CPF, CDA, execução fiscal, guia). O
`@interceptor` de `pgm_api`, por onde a mesma falha sobe, copiava o `data`. A
redação por padrão do interceptor não reconhece credencial nem o formato de
CDA, execução fiscal ou guia.

Invariantes cobertos aqui, sempre com o `internal_request` e o
`InterceptedHTTPClient` reais, o envio real ao interceptor e conferindo todos
os reports que saem:
- nas falhas do caso do CHATR-297 (HTML de 504 e JSON inválido), de timeout e
  de conexão, na obtenção do token e na consulta, nenhum report leva a chave, o
  token nem os dados do contribuinte;
- o report do `internal_request` leva a URL, o método e a forma de
  `request_kwargs` (nomes de campos e de cabeçalhos e contagens); o de
  `pgm_api` leva `resumir_parametros_pgm(data)`;
- o fluxo não muda: a requisição de saída continua com a chave, o token e os
  dados, e a exceção (ou o retorno de indisponibilidade) é o mesmo de antes;
- extrator que falha cai no nome e no tipo dos argumentos, sem valor.
"""

import json

import httpx
import pytest

from src.config import env
from src.tools import divida_ativa, utils
from src.utils import error_interceptor

# Capturado no import, antes do fixture autouse `block_real_error_interceptor`
# (src/tests/unit/conftest.py) trocar o atributo por um mock: os testes precisam
# do payload real que sairia do processo.
_real_send_error_to_interceptor = error_interceptor.send_error_to_interceptor

INTEGRATIONS_URL = "https://integrations.dev488.local/api"
INTERCEPTOR_URL = "https://interceptor.dev488.local/api"
PGM_URL = "https://pgm.dev488.local/api"

# Dados sintéticos.
CHAVE_DE_ACESSO = "chave-sintetica-Qx7Zp4Lm9Wv2"
TOKEN_DA_PGM = "tok3nSinteticoDaPgmKj8Hn5Rt1"
CPF = "11144477735"
CDA = "77/123456/2026-11"
EF = "9999999-99.2099.8.19.0001"
GUIA = "88776655"
TRECHOS_PROIBIDOS = (
    CHAVE_DE_ACESSO,
    "Qx7Zp4Lm9Wv2",
    TOKEN_DA_PGM,
    "Kj8Hn5Rt1",
    CPF,
    "111.444.777-35",
    CDA,
    "123456/2026",
    EF,
    "9999999-99",
    GUIA,
)

ENDPOINT = "v2/guiapagamento/emitir/regularizacao"
CONSUMIDOR = "emitir-guia-regularizacao"
DADOS_DA_CONSULTA = {
    "origem_solicitação": 0,
    "cpfCnpj": CPF,
    "cdas": [CDA],
    "efs": [EF],
    "guias": [GUIA],
}

HTML_504 = "<html><head><title>504 Gateway Time-out</title></head></html>"
HTML_502_SEM_TIMEOUT = "<html><body>erro inesperado do proxy</body></html>"
RESPOSTA_DO_TOKEN = {"access_token": TOKEN_DA_PGM}


class _CapturingTransport(httpx.AsyncBaseTransport):
    """Guarda os envios ao interceptor e responde às chamadas ao
    `chatbot-integrations` com o que estiver em `respostas`, na ordem: uma
    exceção é levantada, texto vira corpo cru, o resto vira corpo JSON."""

    def __init__(self):
        self.reports = []
        self.chamadas = []
        self.respostas = []

    async def handle_async_request(self, request):
        if str(request.url).startswith(INTEGRATIONS_URL):
            self.chamadas.append(json.loads(request.content.decode("utf-8")))
            resposta = self.respostas.pop(0)
            if isinstance(resposta, Exception):
                raise resposta
            if isinstance(resposta, str):
                return httpx.Response(504, text=resposta)
            return httpx.Response(200, json=resposta)
        self.reports.append(request)
        return httpx.Response(200, json={"status": "ok"})


@pytest.fixture
def transport(monkeypatch):
    """Envio real ao interceptor, com o HTTP capturado em memória."""
    capturing = _CapturingTransport()
    monkeypatch.setattr(env, "CHATBOT_INTEGRATIONS_URL", INTEGRATIONS_URL)
    monkeypatch.setattr(env, "CHATBOT_INTEGRATIONS_KEY", "chave-do-proxy-de-teste")
    monkeypatch.setattr(env, "CHATBOT_PGM_API_URL", PGM_URL)
    monkeypatch.setattr(env, "CHATBOT_PGM_ACCESS_KEY", CHAVE_DE_ACESSO)
    monkeypatch.setattr(error_interceptor.env, "ERROR_INTERCEPTOR_URL", INTERCEPTOR_URL)
    monkeypatch.setattr(error_interceptor.env, "ERROR_INTERCEPTOR_TOKEN", "t")
    monkeypatch.setattr(
        error_interceptor,
        "send_error_to_interceptor",
        _real_send_error_to_interceptor,
    )

    class _AsyncClientCapturado(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = capturing
            super().__init__(*args, **kwargs)

    # O mesmo `httpx.AsyncClient` serve ao interceptor e ao
    # `InterceptedHTTPClient` do `internal_request`. Subclasse, e não função,
    # porque o cliente HTTP confere o tipo com `isinstance`.
    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClientCapturado)
    return capturing


def _payloads(transport: _CapturingTransport) -> list:
    """Todos os reports enviados, conferindo que nenhum leva dado proibido."""
    payloads = [json.loads(r.content.decode("utf-8")) for r in transport.reports]
    for payload in payloads:
        corpo = json.dumps(payload, ensure_ascii=False)
        for trecho in TRECHOS_PROIBIDOS:
            assert trecho not in corpo, f"dado proibido no report: {trecho!r}"
    return payloads


def _report_da_funcao(payloads: list, funcao: str) -> dict:
    encontrados = [p for p in payloads if f"function={funcao}" in p["flowname"]]
    assert len(encontrados) == 1, [p["flowname"] for p in payloads]
    return encontrados[0]


FORMA_DO_TOKEN = {
    "url": f"{PGM_URL}/security/token",
    "method": "POST",
    "request_kwargs": {
        "verify": False,
        "headers": {"campos": []},
        "data": {"campos": ["ChaveAcesso", "Consumidor", "grant_type"]},
    },
}

FORMA_DA_CONSULTA = {
    "url": f"{PGM_URL}/{ENDPOINT}",
    "method": "POST",
    "request_kwargs": {
        "verify": False,
        "headers": {"campos": ["Authorization"]},
        "data": {
            "campos": ["cdas", "cpfCnpj", "efs", "guias", "origem_solicitação"],
            "qtd_cdas": 1,
            "qtd_efs": 1,
            "qtd_guias": 1,
        },
    },
}

RESUMO_DO_PGM_API = {
    "endpoint": ENDPOINT,
    "consumidor": CONSUMIDOR,
    "data": {
        "campos": ["cdas", "cpfCnpj", "efs", "guias", "origem_solicitação"],
        "qtd_cdas": 1,
        "qtd_efs": 1,
        "qtd_guias": 1,
    },
}


async def _chamar_pgm_api():
    return await divida_ativa.pgm_api(
        endpoint=ENDPOINT, consumidor=CONSUMIDOR, data=dict(DADOS_DA_CONSULTA)
    )


def _assert_requisicoes_de_saida_intactas(transport, chamadas_esperadas: int) -> None:
    """O fluxo não muda: o proxy continua recebendo a chave, o token e os dados."""
    assert len(transport.chamadas) == chamadas_esperadas
    token = transport.chamadas[0]
    assert token["url"] == f"{PGM_URL}/security/token"
    assert token["request_kwargs"]["data"]["ChaveAcesso"] == CHAVE_DE_ACESSO
    if chamadas_esperadas > 1:
        consulta = transport.chamadas[1]
        assert consulta["url"] == f"{PGM_URL}/{ENDPOINT}"
        assert consulta["request_kwargs"]["headers"] == {
            "Authorization": f"Bearer {TOKEN_DA_PGM}"
        }
        assert consulta["request_kwargs"]["data"] == DADOS_DA_CONSULTA


# ---------------------------------------------------------------------------
# Obtenção do token (ChaveAcesso)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_html_de_504_no_token_nao_envia_a_chave_de_acesso(transport):
    """Caso do CHATR-297: o proxy devolve HTML de 504 e o `internal_request`
    levanta `TimeoutError`, que o `pgm_api` transforma em indisponibilidade."""
    transport.respostas = [HTML_504]

    resultado = await _chamar_pgm_api()

    assert resultado["erro"] is True
    assert "temporariamente indisponível" in resultado["motivos"]
    _assert_requisicoes_de_saida_intactas(transport, 1)

    payloads = _payloads(transport)
    report = _report_da_funcao(payloads, "internal_request")
    assert json.loads(report["input_body"]) == FORMA_DO_TOKEN
    assert "TimeoutError" in report["api_endpoint"]


@pytest.mark.asyncio
async def test_json_invalido_no_token_nao_envia_a_chave_de_acesso(transport):
    transport.respostas = [HTML_502_SEM_TIMEOUT]

    with pytest.raises(json.JSONDecodeError):
        await _chamar_pgm_api()

    _assert_requisicoes_de_saida_intactas(transport, 1)
    payloads = _payloads(transport)
    assert json.loads(
        _report_da_funcao(payloads, "internal_request")["input_body"]
    ) == (FORMA_DO_TOKEN)
    assert (
        json.loads(_report_da_funcao(payloads, "pgm_api")["input_body"])
        == RESUMO_DO_PGM_API
    )


# ---------------------------------------------------------------------------
# Consulta à PGM (token Bearer e dados do contribuinte)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_html_de_504_na_consulta_nao_envia_token_nem_contribuinte(transport):
    transport.respostas = [RESPOSTA_DO_TOKEN, HTML_504]

    resultado = await _chamar_pgm_api()

    assert resultado["erro"] is True
    _assert_requisicoes_de_saida_intactas(transport, 2)
    report = _report_da_funcao(_payloads(transport), "internal_request")
    assert json.loads(report["input_body"]) == FORMA_DA_CONSULTA


@pytest.mark.asyncio
async def test_json_invalido_na_consulta_nao_envia_token_nem_contribuinte(
    transport,
):
    """A falha sobe pelos dois decorators: o do `internal_request` e o do
    `pgm_api`. Nenhum dos dois leva o token nem o `data`."""
    transport.respostas = [RESPOSTA_DO_TOKEN, HTML_502_SEM_TIMEOUT]

    with pytest.raises(json.JSONDecodeError):
        await _chamar_pgm_api()

    _assert_requisicoes_de_saida_intactas(transport, 2)
    payloads = _payloads(transport)
    assert len(payloads) == 2
    assert (
        json.loads(_report_da_funcao(payloads, "internal_request")["input_body"])
        == FORMA_DA_CONSULTA
    )
    assert (
        json.loads(_report_da_funcao(payloads, "pgm_api")["input_body"])
        == RESUMO_DO_PGM_API
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "falha, tipo",
    [
        (httpx.ConnectError("proxy recusou a conexão"), httpx.ConnectError),
        (httpx.ReadError("proxy fechou a conexão"), httpx.ReadError),
    ],
    ids=["conexao", "leitura"],
)
async def test_falha_de_transporte_na_consulta_nao_envia_token_nem_contribuinte(
    transport, falha, tipo
):
    """Além dos dois decorators, o `InterceptedHTTPClient` também reporta: o
    corpo vai como `content` e não chega ao report dele."""
    transport.respostas = [RESPOSTA_DO_TOKEN, falha]

    with pytest.raises(tipo):
        await _chamar_pgm_api()

    _assert_requisicoes_de_saida_intactas(transport, 2)
    payloads = _payloads(transport)
    assert len(payloads) == 3
    assert (
        json.loads(_report_da_funcao(payloads, "internal_request")["input_body"])
        == FORMA_DA_CONSULTA
    )
    assert (
        json.loads(_report_da_funcao(payloads, "pgm_api")["input_body"])
        == RESUMO_DO_PGM_API
    )


@pytest.mark.asyncio
async def test_timeout_na_consulta_nao_envia_token_nem_contribuinte(transport):
    transport.respostas = [RESPOSTA_DO_TOKEN, httpx.ReadTimeout("read timeout")]

    resultado = await _chamar_pgm_api()

    # A mensagem cita "timeout": o `pgm_api` responde indisponibilidade.
    assert resultado["erro"] is True
    _assert_requisicoes_de_saida_intactas(transport, 2)
    report = _report_da_funcao(_payloads(transport), "internal_request")
    assert json.loads(report["input_body"]) == FORMA_DA_CONSULTA


# ---------------------------------------------------------------------------
# Extratores
# ---------------------------------------------------------------------------


def test_input_body_do_internal_request_por_posicao_e_sem_kwargs():
    url = f"{PGM_URL}/security/token?apikey={CHAVE_DE_ACESSO}"
    por_posicao = utils._input_body_do_internal_request(
        (url, "POST", {"data": {"ChaveAcesso": CHAVE_DE_ACESSO}, "timeout": 30}),
        {},
    )
    assert por_posicao == {
        "url": f"{PGM_URL}/security/token?apikey=<redacted>",
        "method": "POST",
        "request_kwargs": {
            "data": {"campos": ["ChaveAcesso"]},
            "timeout": {"tipo": "int"},
        },
    }

    so_a_url = utils._input_body_do_internal_request((), {"url": PGM_URL})
    assert so_a_url == {"url": PGM_URL, "method": "GET", "request_kwargs": None}

    kwargs_estranhos = utils._input_body_do_internal_request(
        (), {"url": 123, "method": None, "request_kwargs": [CPF, CDA]}
    )
    assert kwargs_estranhos == {
        "url": None,
        "method": None,
        "request_kwargs": {"tipo": "lista", "qtd": 2},
    }
    assert CPF not in json.dumps(kwargs_estranhos)


def test_input_body_do_pgm_api_por_posicao_e_data_ausente():
    por_posicao = divida_ativa._input_body_do_pgm_api(
        (ENDPOINT, CONSUMIDOR, dict(DADOS_DA_CONSULTA)), {}
    )
    assert por_posicao == RESUMO_DO_PGM_API

    sem_data = divida_ativa._input_body_do_pgm_api((), {"endpoint": ENDPOINT})
    assert sem_data == {
        "endpoint": ENDPOINT,
        "consumidor": "",
        "data": {"campos": []},
    }

    tipos_estranhos = divida_ativa._input_body_do_pgm_api((1, None, [CPF]), {})
    assert tipos_estranhos == {
        "endpoint": None,
        "consumidor": None,
        "data": {"tipo": "list"},
    }


@pytest.mark.asyncio
async def test_extrator_que_falha_nao_envia_valor(transport, monkeypatch):
    """Se o extrator levantar, o decorator manda só o nome e o tipo de cada
    argumento (DEV-278), nunca o `request_kwargs` cru."""

    def _forma_que_falha(_valor):
        raise RuntimeError("falha no resumo")

    monkeypatch.setattr(utils, "_forma_do_valor", _forma_que_falha)
    transport.respostas = [HTML_502_SEM_TIMEOUT]

    with pytest.raises(json.JSONDecodeError):
        await _chamar_pgm_api()

    report = _report_da_funcao(_payloads(transport), "internal_request")
    assert json.loads(report["input_body"]) == {
        "url": "str",
        "method": "str",
        "request_kwargs": "dict",
    }
