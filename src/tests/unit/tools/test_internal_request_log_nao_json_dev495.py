"""
Testes da DEV-495: a resposta não-JSON do `chatbot-integrations` não vai para
o log do `internal_request`.

Antes, o ramo `except json.JSONDecodeError` logava `text[:500]`: os primeiros
500 caracteres do corpo devolvido (HTML de 502/504 do proxy, como no CHATR-297,
ou uma resposta de texto da PGM). A redação global de PII (`src/utils/log.py`)
só reconhece dado com formato (CPF, CNPJ, telefone, e-mail): CDA, execução
fiscal, guia e nome passavam.

Invariantes cobertos aqui, com o `internal_request` real e o logger real
(sink do loguru capturando todas as linhas, em qualquer nível):
- nenhuma linha de log leva trecho do corpo, no caminho de erro genérico, no de
  gateway/timeout e no de texto puro;
- o log registra só a forma da resposta (`resumir_resposta_nao_json`): status
  HTTP, tamanho, tipo de conteúdo sem parâmetros, se parece HTML e se parece
  erro de gateway/timeout;
- o fluxo não muda: 502/504/timeout viram `TimeoutError`, o resto relança o
  `JSONDecodeError`.
"""

import json
from types import SimpleNamespace

import httpx
import pytest
from loguru import logger as loguru_logger

from src.tools import utils

# Dados sintéticos.
NOME = "Fulano Sintetico de Tal"
CDA = "77/123456/2026-11"
EF = "9999999-99.2099.8.19.0001"
GUIA = "88776655"
TRECHOS_PROIBIDOS = (
    NOME,
    "Sintetico",
    CDA,
    "123456/2026",
    EF,
    "9999999-99",
    GUIA,
    "erro inesperado do proxy",
)

HTML_502_SEM_TIMEOUT = (
    "<!DOCTYPE html><html><body>erro inesperado do proxy para "
    f"{NOME}, CDA {CDA}, EF {EF}, guia {GUIA}</body></html>"
)
HTML_504 = (
    "<html><head><title>504 Gateway Time-out</title></head>"
    f"<body>{NOME} {CDA} {EF} {GUIA}</body></html>"
)
TEXTO_DA_PGM = f"Contribuinte {NOME} com CDA {CDA}, EF {EF} e guia {GUIA}"


class _ClienteFalso:
    def __init__(self, resposta):
        self.resposta = resposta

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url, **kwargs):
        return self.resposta


@pytest.fixture
def linhas_de_log():
    linhas: list[str] = []
    sink_id = loguru_logger.add(
        lambda mensagem: linhas.append(str(mensagem)),
        format="{message}",
        level="TRACE",
    )
    try:
        yield linhas
    finally:
        loguru_logger.remove(sink_id)


@pytest.fixture(autouse=True)
def _integrations_falso(monkeypatch):
    monkeypatch.setattr(
        utils.env, "CHATBOT_INTEGRATIONS_URL", "https://integrations.dev495.local"
    )
    monkeypatch.setattr(utils.env, "CHATBOT_INTEGRATIONS_KEY", "chave-sintetica")


def _usar_resposta(monkeypatch, resposta):
    monkeypatch.setattr(
        utils, "InterceptedHTTPClient", lambda **_kwargs: _ClienteFalso(resposta)
    )


def _sem_trecho_do_corpo(linhas: list[str]) -> None:
    texto = "\n".join(linhas)
    for trecho in TRECHOS_PROIBIDOS:
        assert trecho not in texto, f"trecho do corpo no log: {trecho!r}"


def _resumo_logado(linhas: list[str]) -> str:
    resumos = [
        linha for linha in linhas if "internal_request_resposta_nao_json" in linha
    ]
    assert len(resumos) == 1, linhas
    return resumos[0]


@pytest.mark.asyncio
async def test_html_de_erro_do_proxy_nao_vai_ao_log(monkeypatch, linhas_de_log):
    resposta = httpx.Response(
        502,
        text=HTML_502_SEM_TIMEOUT,
        headers={"content-type": "text/html; charset=utf-8"},
    )
    _usar_resposta(monkeypatch, resposta)

    with pytest.raises(json.JSONDecodeError):
        await utils.internal_request("https://pgm.dev495.local/api/consulta")

    _sem_trecho_do_corpo(linhas_de_log)
    resumo = _resumo_logado(linhas_de_log)
    assert "'status_code': 502" in resumo
    assert f"'tamanho': {len(HTML_502_SEM_TIMEOUT)}" in resumo
    assert "'content_type': 'text/html'" in resumo
    assert "'parece_html': True" in resumo
    assert "'parece_gateway_timeout': False" in resumo


@pytest.mark.asyncio
async def test_html_de_504_vira_timeout_sem_ir_ao_log(monkeypatch, linhas_de_log):
    resposta = httpx.Response(504, text=HTML_504, headers={"content-type": "text/html"})
    _usar_resposta(monkeypatch, resposta)

    with pytest.raises(TimeoutError, match="Gateway timeout"):
        await utils.internal_request("https://pgm.dev495.local/api/consulta")

    _sem_trecho_do_corpo(linhas_de_log)
    resumo = _resumo_logado(linhas_de_log)
    assert "'status_code': 504" in resumo
    assert "'parece_html': True" in resumo
    assert "'parece_gateway_timeout': True" in resumo


@pytest.mark.asyncio
async def test_texto_puro_da_pgm_nao_vai_ao_log(monkeypatch, linhas_de_log):
    resposta = httpx.Response(
        200, text=TEXTO_DA_PGM, headers={"content-type": "text/plain; charset=utf-8"}
    )
    _usar_resposta(monkeypatch, resposta)

    with pytest.raises(json.JSONDecodeError):
        await utils.internal_request("https://pgm.dev495.local/api/consulta")

    _sem_trecho_do_corpo(linhas_de_log)
    resumo = _resumo_logado(linhas_de_log)
    assert "'status_code': 200" in resumo
    assert "'content_type': 'text/plain'" in resumo
    assert "'parece_html': False" in resumo
    assert "'parece_gateway_timeout': False" in resumo


def test_resumo_aceita_resposta_sem_status_nem_cabecalhos():
    resumo = utils.resumir_resposta_nao_json(SimpleNamespace(), TEXTO_DA_PGM)

    assert resumo == {
        "status_code": None,
        "tamanho": len(TEXTO_DA_PGM),
        "content_type": None,
        "parece_html": False,
        "parece_gateway_timeout": False,
    }


def test_resumo_nao_copia_content_type_estranho():
    content_type = "text/html; " + "x" * 500
    resposta = SimpleNamespace(
        status_code=502, headers={"content-type": content_type.upper()}
    )

    resumo = utils.resumir_resposta_nao_json(resposta, "<html></html>")

    assert resumo["content_type"] == "text/html"
    assert resumo["parece_html"] is True
