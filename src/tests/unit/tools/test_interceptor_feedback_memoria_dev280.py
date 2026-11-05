"""
Testes da DEV-280: `store_user_feedback` e `upsert_memory` não mandam o texto
do cidadão no `input_body` do `@interceptor` quando falham.

Antes, o decorator copiava os argumentos `str` e `dict`: o feedback inteiro e o
banco de memórias inteiro (o que o cidadão contou sobre saúde, família,
endereço) iam ao error interceptor externo, só com a redação por padrão.

Invariantes cobertos aqui:
- o feedback vira `resumir_texto_livre` (tamanho e tipos de dado pessoal) em
  `store_user_feedback` e em `save_feedback_in_bq` (a gravação no BigQuery que
  falha, o caminho real);
- o `memory_bank` vira os nomes das chaves e o tamanho de cada valor, por
  posição ou por nome; um `memory_bank` que não é dict vira só o tipo;
- o `user_id` não entra no `input_body` do `upsert_memory` (vai mascarado no
  campo próprio do report, `customer_whatsapp_number`);
- `get_memories`, sem texto livre, segue com o `input_body` de antes.
"""

import json

import httpx
import pytest

from src.tools import feedback_tools, memory
from src.utils import bigquery, error_interceptor
from src.utils.pii import resumir_texto_livre

# Capturado no import, antes do fixture autouse `block_real_error_interceptor`
# (src/tests/unit/conftest.py) trocar o atributo por um mock: os testes precisam
# do payload real que sairia do processo.
_real_send_error_to_interceptor = error_interceptor.send_error_to_interceptor

# Dados sintéticos. O CPF é válido no dígito verificador para a redação casar;
# o contexto de saúde é o que nenhuma redação por padrão apaga.
CPF_SINTETICO = "529.982.247-25"
USER_ID = "usuario-sintetico-280"
FEEDBACK = f"o atendimento sobre o tratamento de hemodialise do meu pai foi ruim, cpf {CPF_SINTETICO}"
MEMORIA_VALOR = "cidada gestante com diabetes, mora com a avo acamada"
MEMORIA_DESCRICAO = "condicao de saude informada na conversa"
MEMORY_BANK = {
    "memory_name": "saude_familia",
    "description": MEMORIA_DESCRICAO,
    "relevance": "high",
    "memory_type": "base",
    "value": MEMORIA_VALOR,
}
TRECHOS_PROIBIDOS = (
    "hemodialise",
    "atendimento",
    CPF_SINTETICO,
    "gestante",
    "diabetes",
    "acamada",
    "condicao de saude",
)


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


def _sem_texto_do_cidadao(payload: dict) -> None:
    corpo = json.dumps(payload, ensure_ascii=False)
    for trecho in TRECHOS_PROIBIDOS:
        assert trecho not in corpo


def _falha(*_args, **_kwargs):
    raise RuntimeError("falha interna da tool")


# ---------------------------------------------------------------------------
# store_user_feedback
# ---------------------------------------------------------------------------


class _LoggerQueFalha:
    """`store_user_feedback` trata as próprias exceções e loga; a exceção só
    escapa ao decorator se o próprio tratamento falhar."""

    def info(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        raise RuntimeError("falha ao logar o erro do feedback")


@pytest.mark.asyncio
async def test_feedback_nao_envia_o_texto_ao_interceptor(transport, monkeypatch):
    monkeypatch.setattr(feedback_tools, "get_datetime", _falha)
    monkeypatch.setattr(feedback_tools, "logger", _LoggerQueFalha())

    with pytest.raises(RuntimeError, match="falha ao logar o erro do feedback"):
        await feedback_tools.store_user_feedback(USER_ID, FEEDBACK)

    payload = _payload_enviado(transport)
    assert json.loads(payload["input_body"]) == {
        "user_id": USER_ID,
        "feedback": resumir_texto_livre(FEEDBACK),
    }
    assert "cpf" in json.loads(payload["input_body"])["feedback"]["pii_redigida"]
    _sem_texto_do_cidadao(payload)


@pytest.mark.asyncio
async def test_feedback_por_nome_tambem_e_resumido(transport, monkeypatch):
    monkeypatch.setattr(feedback_tools, "get_datetime", _falha)
    monkeypatch.setattr(feedback_tools, "logger", _LoggerQueFalha())

    with pytest.raises(RuntimeError):
        await feedback_tools.store_user_feedback(user_id=USER_ID, feedback=FEEDBACK)

    payload = _payload_enviado(transport)
    assert json.loads(payload["input_body"])["feedback"] == resumir_texto_livre(
        FEEDBACK
    )
    _sem_texto_do_cidadao(payload)


# ---------------------------------------------------------------------------
# save_feedback_in_bq: o caminho real da falha ao gravar o feedback
# ---------------------------------------------------------------------------


def _gravacao_que_falha(*_args, **_kwargs):
    raise RuntimeError("falha ao gravar no BigQuery")


@pytest.mark.asyncio
async def test_falha_ao_gravar_feedback_no_bigquery_nao_envia_o_texto(
    transport, monkeypatch
):
    """Caminho de produção: `store_user_feedback` dispara
    `save_feedback_in_bq_background`, que roda `save_feedback_in_bq` numa
    thread do executor; a falha da gravação é reportada pelo `@interceptor`
    dela e engolida pelo wrapper de background."""
    monkeypatch.setattr(bigquery, "_gravar_linha", _gravacao_que_falha)

    await bigquery.save_feedback_in_bq_background(
        user_id=USER_ID,
        feedback=FEEDBACK,
        timestamp="2026-11-05T12:00:00",
        environment="staging",
    )

    payload = _payload_enviado(transport)
    input_body = json.loads(payload["input_body"])
    assert input_body["feedback"] == resumir_texto_livre(FEEDBACK)
    assert "cpf" in input_body["feedback"]["pii_redigida"]
    assert input_body["environment"] == "staging"
    assert "falha ao gravar no BigQuery" in payload["error_response"]
    _sem_texto_do_cidadao(payload)


def test_save_feedback_in_bq_sync_por_nome_nao_envia_o_texto(transport, monkeypatch):
    monkeypatch.setattr(bigquery, "_gravar_linha", _gravacao_que_falha)

    with pytest.raises(RuntimeError, match="falha ao gravar no BigQuery"):
        bigquery.save_feedback_in_bq(
            user_id=USER_ID,
            feedback=FEEDBACK,
            timestamp="2026-11-05T12:00:00",
            environment="staging",
        )

    payload = _payload_enviado(transport)
    assert json.loads(payload["input_body"])["feedback"] == resumir_texto_livre(
        FEEDBACK
    )
    _sem_texto_do_cidadao(payload)


# ---------------------------------------------------------------------------
# upsert_memory
# ---------------------------------------------------------------------------


@pytest.fixture
def memoria_com_oauth(monkeypatch):
    async def authorization_header():
        return "Bearer token-de-teste"

    monkeypatch.setattr(memory, "is_oauth2_configured", lambda: True)
    monkeypatch.setattr(memory, "get_authorization_header", authorization_header)
    monkeypatch.setattr(memory, "InterceptedHTTPClient", _falha)


RESUMO_DO_MEMORY_BANK = {
    "memory_bank": {
        "memory_name": {"chars": len("saude_familia")},
        "description": {"chars": len(MEMORIA_DESCRICAO)},
        "relevance": {"chars": len("high")},
        "memory_type": {"chars": len("base")},
        "value": {"chars": len(MEMORIA_VALOR)},
    }
}


@pytest.mark.asyncio
async def test_upsert_memory_envia_so_nomes_e_tamanhos(transport, memoria_com_oauth):
    with pytest.raises(RuntimeError, match="falha interna da tool"):
        await memory.upsert_memory(USER_ID, dict(MEMORY_BANK))

    payload = _payload_enviado(transport)
    assert json.loads(payload["input_body"]) == RESUMO_DO_MEMORY_BANK
    assert "customer_whatsapp_number" in payload
    _sem_texto_do_cidadao(payload)


@pytest.mark.asyncio
async def test_upsert_memory_por_nome(transport, memoria_com_oauth):
    with pytest.raises(RuntimeError, match="falha interna da tool"):
        await memory.upsert_memory(user_id=USER_ID, memory_bank=dict(MEMORY_BANK))

    payload = _payload_enviado(transport)
    assert json.loads(payload["input_body"]) == RESUMO_DO_MEMORY_BANK
    _sem_texto_do_cidadao(payload)


@pytest.mark.asyncio
async def test_upsert_memory_que_nao_e_dict_vai_so_com_o_tipo(transport):
    """`MemoryBank(**lista)` levanta TypeError, que não é ValidationError e
    escapa ao decorator."""
    with pytest.raises(TypeError):
        await memory.upsert_memory(USER_ID, [MEMORIA_VALOR, MEMORIA_DESCRICAO])

    payload = _payload_enviado(transport)
    assert json.loads(payload["input_body"]) == {"memory_bank": {"tipo": "list"}}
    _sem_texto_do_cidadao(payload)


def test_resumo_do_memory_bank_corta_nome_longo_e_nao_leva_valor():
    chave_longa = "k" * 200
    resumo = memory._input_body_do_upsert(
        (USER_ID,), {"memory_bank": {chave_longa: MEMORIA_VALOR, "n": 3}}
    )

    assert resumo == {
        "memory_bank": {
            "k" * 64: {"chars": len(MEMORIA_VALOR)},
            "n": {"tipo": "int"},
        }
    }


# ---------------------------------------------------------------------------
# get_memories: sem texto livre, sem mudança
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_memories_mantem_o_input_body(transport, memoria_com_oauth):
    with pytest.raises(RuntimeError, match="falha interna da tool"):
        await memory.get_memories(USER_ID, "saude_familia")

    payload = _payload_enviado(transport)
    assert json.loads(payload["input_body"]) == {
        "user_id": USER_ID,
        "memory_name": "saude_familia",
    }
