"""
Testes da DEV-278: o `@interceptor` não copia a pergunta do cidadão no
`input_body` enviado ao error interceptor externo.

Antes, `_handle_error` montava o `input_body` com todos os argumentos `str`,
`dict` e `list` da função que falhou. Nas tools de busca isso é a pergunta do
cidadão inteira, e a redação por padrão (CPF, telefone) não apaga o contexto
livre: doença, família, dívida.

Invariantes cobertos aqui:
- `free_text_params` troca o argumento por tamanho e tipos de dado pessoal
  (`resumir_texto_livre`), nunca o texto, em funções async e sync.
- `extract_input_body` define o `input_body`; se ele falhar, vão só o nome e o
  tipo de cada argumento, e o warning não traz a mensagem da exceção.
- `get_google_search`, `GeminiService.google_search`, `dharma_search` e
  `surkai_search` reportam só o resumo da pergunta.
- Sem configuração, o `input_body` continua igual ao de antes.
"""

import asyncio
import contextlib
import json

import httpx
import pytest

# As tools são importadas antes de o fixture `transport` trocar
# `httpx.AsyncClient` por uma função: o SDK do Gemini herda dessa classe no
# import.
from src.tools import dharma_search, search, web_search_surkai
from src.tools.google_search.gemini_service import GeminiService
from src.utils import error_interceptor
from src.utils.pii import resumir_texto_livre

# Capturado no import, antes do fixture autouse `block_real_error_interceptor`
# (src/tests/unit/conftest.py) substituir o atributo por um mock: os testes
# precisam do payload real que sairia do processo.
_real_send_error_to_interceptor = error_interceptor.send_error_to_interceptor

# Dados sintéticos. O CPF é válido no dígito verificador para a redação casar;
# o contexto de saúde é o que nenhuma redação por padrão apaga.
CPF_SINTETICO = "529.982.247-25"
PERGUNTA = (
    f"meu filho tem leucemia e preciso do remedio de alto custo, cpf {CPF_SINTETICO}"
)
CONTEXTO = "mae solo endividada pedindo ajuda com tratamento oncologico"
TRECHOS_PROIBIDOS = ("leucemia", "remedio", CPF_SINTETICO, "endividada", "oncologico")


class _CapturingTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.requests = []

    async def handle_async_request(self, request):
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
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = capturing
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(error_interceptor.httpx, "AsyncClient", fake_async_client)
    return capturing


def _payload_enviado(transport: _CapturingTransport) -> dict:
    assert len(transport.requests) == 1
    return json.loads(transport.requests[0].content.decode("utf-8"))


def _sem_texto_da_pergunta(payload: dict) -> None:
    corpo = json.dumps(payload, ensure_ascii=False)
    for trecho in TRECHOS_PROIBIDOS:
        assert trecho not in corpo


@contextlib.contextmanager
def _captura_warnings():
    registros = []
    handler_id = error_interceptor.logger.add(
        lambda mensagem: registros.append(mensagem.record),
        level="WARNING",
        format="{message}",
    )
    try:
        yield registros
    finally:
        error_interceptor.logger.remove(handler_id)


# ---------------------------------------------------------------------------
# resumir_texto_livre
# ---------------------------------------------------------------------------


def test_resumo_traz_tamanho_e_tipos_nunca_o_texto():
    resumo = resumir_texto_livre(PERGUNTA)

    assert resumo["chars"] == len(PERGUNTA)
    assert "cpf" in resumo["pii_redigida"]
    assert set(resumo) == {"chars", "pii_redigida"}
    _sem_texto_da_pergunta(resumo)


def test_resumo_de_texto_ja_redigido_le_os_marcadores():
    assert resumir_texto_livre("cpf [cpf] e telefone [telefone]") == {
        "chars": len("cpf [cpf] e telefone [telefone]"),
        "pii_redigida": ["cpf", "telefone"],
    }


def test_resumo_de_vazio_e_de_valor_que_nao_e_texto():
    assert resumir_texto_livre(None) == {"chars": 0, "pii_redigida": []}
    assert resumir_texto_livre("") == {"chars": 0, "pii_redigida": []}
    assert resumir_texto_livre(["lista", "com", "texto"]) == {"tipo": "list"}


# ---------------------------------------------------------------------------
# Decorator: free_text_params e extract_input_body
# ---------------------------------------------------------------------------


@error_interceptor.interceptor(
    source={"source": "mcp", "tool": "teste"}, free_text_params=("pergunta",)
)
async def _busca_async(pergunta: str, limite: int = 5):
    raise RuntimeError("falha no pós-processamento")


@error_interceptor.interceptor(
    source={"source": "mcp", "tool": "teste"}, free_text_params="pergunta"
)
def _busca_sync(pergunta: str, limite: int = 5):
    raise RuntimeError("falha sync")


@pytest.mark.asyncio
async def test_free_text_params_resume_o_argumento_e_mantem_os_demais(transport):
    with pytest.raises(RuntimeError, match="falha no pós-processamento"):
        await _busca_async(PERGUNTA, limite=3)

    payload = _payload_enviado(transport)
    assert json.loads(payload["input_body"]) == {
        "pergunta": resumir_texto_livre(PERGUNTA),
        "limite": 3,
    }
    _sem_texto_da_pergunta(payload)


@pytest.mark.asyncio
async def test_free_text_params_vale_por_nome_em_kwargs(transport):
    with pytest.raises(RuntimeError):
        await _busca_async(pergunta=PERGUNTA)

    payload = _payload_enviado(transport)
    assert json.loads(payload["input_body"]) == {
        "pergunta": resumir_texto_livre(PERGUNTA)
    }
    _sem_texto_da_pergunta(payload)


@pytest.mark.asyncio
async def test_free_text_params_como_string_e_funcao_sync(transport):
    """Uma string sozinha é um nome, não uma sequência de letras: "pergunta"
    não pode virar o conjunto {"p", "e", ...} e deixar o argumento passar."""
    antes = set(error_interceptor._pending_interceptor_tasks)

    with pytest.raises(RuntimeError, match="falha sync"):
        _busca_sync(PERGUNTA)

    novas = error_interceptor._pending_interceptor_tasks - antes
    await asyncio.gather(*novas)

    payload = _payload_enviado(transport)
    assert json.loads(payload["input_body"]) == {
        "pergunta": resumir_texto_livre(PERGUNTA)
    }
    _sem_texto_da_pergunta(payload)


@pytest.mark.asyncio
async def test_extract_input_body_define_o_que_vai_ao_interceptor(transport):
    @error_interceptor.interceptor(
        source={"source": "mcp", "tool": "teste"},
        extract_input_body=lambda args, kwargs: {"tamanho": len(args[0])},
    )
    async def busca(pergunta: str):
        raise ValueError("falhou")

    with pytest.raises(ValueError, match="falhou"):
        await busca(PERGUNTA)

    payload = _payload_enviado(transport)
    assert json.loads(payload["input_body"]) == {"tamanho": len(PERGUNTA)}
    _sem_texto_da_pergunta(payload)


@pytest.mark.asyncio
async def test_extract_input_body_que_falha_manda_so_nome_e_tipo(transport):
    def extrator_quebrado(args, kwargs):
        raise KeyError(f"sem chave em {args[0]}")

    @error_interceptor.interceptor(
        source={"source": "mcp", "tool": "teste"},
        extract_input_body=extrator_quebrado,
    )
    async def busca(pergunta: str, limite: int = 5, filtros=None):
        raise ValueError("falha original")

    with _captura_warnings() as registros:
        with pytest.raises(ValueError, match="falha original"):
            await busca(PERGUNTA, limite=2, filtros=object())

    payload = _payload_enviado(transport)
    assert json.loads(payload["input_body"]) == {
        "pergunta": "str",
        "limite": "int",
        "filtros": "object",
    }
    _sem_texto_da_pergunta(payload)
    avisos = [r for r in registros if "extract_input_body" in r["message"]]
    assert len(avisos) == 1
    assert "KeyError" in avisos[0]["message"]
    assert avisos[0]["exception"] is None
    for trecho in TRECHOS_PROIBIDOS:
        assert trecho not in avisos[0]["message"]


@pytest.mark.asyncio
async def test_sem_configuracao_o_input_body_continua_igual(transport):
    """As demais tools não mudam: tipos simples com o valor, objeto com o nome
    da classe, sem `self`."""

    class Servico:
        @error_interceptor.interceptor(source={"source": "mcp", "tool": "teste"})
        async def consultar(self, inscricao: str, filtros: dict, cliente=None):
            raise RuntimeError("falhou")

    with pytest.raises(RuntimeError):
        await Servico().consultar("12345678", {"ano": 2026}, cliente=object())

    payload = _payload_enviado(transport)
    assert json.loads(payload["input_body"]) == {
        "inscricao": "12345678",
        "filtros": {"ano": 2026},
        "cliente": "object",
    }


# ---------------------------------------------------------------------------
# Tools de busca
# ---------------------------------------------------------------------------


def _falha(*_args, **_kwargs):
    raise RuntimeError("falha interna da busca")


@pytest.mark.asyncio
async def test_get_google_search_nao_envia_a_pergunta_nem_o_contexto(
    transport, monkeypatch
):
    # Falha depois do filtro, antes de qualquer buscador: exceção não tratada
    # dentro da tool, o caso que o decorator reporta.
    monkeypatch.setattr(search, "_marcar_filtro_no_span", _falha)

    with pytest.raises(RuntimeError, match="falha interna da busca"):
        await search.get_google_search(PERGUNTA, contexto=CONTEXTO)

    payload = _payload_enviado(transport)
    assert json.loads(payload["input_body"]) == {
        "query": resumir_texto_livre(PERGUNTA),
        "contexto": resumir_texto_livre(CONTEXTO),
    }
    _sem_texto_da_pergunta(payload)


@pytest.mark.asyncio
async def test_gemini_google_search_nao_envia_a_query(transport, monkeypatch):
    monkeypatch.setattr(GeminiService, "_resumo_da_query", staticmethod(_falha))
    servico = object.__new__(GeminiService)

    with pytest.raises(RuntimeError, match="falha interna da busca"):
        await servico.google_search(PERGUNTA)

    payload = _payload_enviado(transport)
    assert json.loads(payload["input_body"]) == {"query": resumir_texto_livre(PERGUNTA)}
    _sem_texto_da_pergunta(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module", "funcao"),
    [(dharma_search, "dharma_search"), (web_search_surkai, "surkai_search")],
)
async def test_buscas_externas_nao_enviam_a_query(
    transport, monkeypatch, module, funcao
):
    monkeypatch.setattr(module, "InterceptedHTTPClient", _falha)

    with pytest.raises(RuntimeError, match="falha interna da busca"):
        await getattr(module, funcao)(PERGUNTA)

    payload = _payload_enviado(transport)
    assert json.loads(payload["input_body"])["query"] == resumir_texto_livre(PERGUNTA)
    _sem_texto_da_pergunta(payload)
