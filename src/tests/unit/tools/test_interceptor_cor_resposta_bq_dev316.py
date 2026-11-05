"""
Testes da DEV-316: a falha ao gravar um alerta do COR ou uma resposta de tool no
BigQuery não manda o texto do cidadão, a coordenada nem os valores da resposta
no `input_body` do `@interceptor`.

Antes, o decorator copiava os argumentos `str`, `float` e `dict`: a descrição
livre do alerta, o endereço, o bairro informado, a latitude e a longitude, e o
`data` inteiro da resposta (com a pergunta do cidadão) iam ao error interceptor
externo, só com a redação por padrão.

Invariantes cobertos aqui:
- em `save_cor_alert_in_bq`, descrição, endereço e bairro informado viram
  `resumir_texto_livre` (tamanho e tipos de dado pessoal); latitude e longitude
  vão só com o tipo; os campos de código (tipo, severidade, ambiente) seguem;
- em `save_response_in_bq`, `data` vira os nomes das chaves e o tamanho de cada
  valor; um `data` que não é dict vira só o tipo e o tamanho; endpoint, tabela e
  ambiente seguem;
- vale pelos wrappers de background (o caminho real), por posição e por nome.
"""

import json

import httpx
import pytest

from src.utils import bigquery, error_interceptor
from src.utils.pii import resumir_texto_livre

# Capturado no import, antes do fixture autouse `block_real_error_interceptor`
# (src/tests/unit/conftest.py) trocar o atributo por um mock: os testes precisam
# do payload real que sairia do processo.
_real_send_error_to_interceptor = error_interceptor.send_error_to_interceptor

# Dados sintéticos. O CPF é válido no dígito verificador para a redação casar.
CPF_SINTETICO = "529.982.247-25"
USER_ID = "usuario-sintetico-316"
DESCRICAO = (
    f"arvore caiu em cima do carro do meu vizinho idoso cadeirante, cpf {CPF_SINTETICO}"
)
ENDERECO = "Rua Sintetica das Palmeiras 4321 fundos"
BAIRRO = "Vila Ficticia do Norte"
LATITUDE = -22.9876543
LONGITUDE = -43.1234567
QUERY = "remedio para depressao do meu filho adolescente"
TRECHOS_PROIBIDOS = (
    "arvore",
    "cadeirante",
    CPF_SINTETICO,
    "Palmeiras",
    "4321",
    "Ficticia",
    "22.98",
    "43.12",
    "9876543",
    "1234567",
    "depressao",
    "adolescente",
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


@pytest.fixture
def gravacao_que_falha(monkeypatch):
    def _falha(*_args, **_kwargs):
        raise RuntimeError("falha ao gravar no BigQuery")

    monkeypatch.setattr(bigquery, "_gravar_linha", _falha)


def _payload_enviado(transport: _CapturingTransport) -> dict:
    assert len(transport.requests) == 1
    return json.loads(transport.requests[0].content.decode("utf-8"))


def _sem_dado_do_cidadao(payload: dict) -> None:
    corpo = json.dumps(payload, ensure_ascii=False)
    for trecho in TRECHOS_PROIBIDOS:
        assert trecho not in corpo


# ---------------------------------------------------------------------------
# save_cor_alert_in_bq
# ---------------------------------------------------------------------------


def _assert_input_body_do_alerta(input_body: dict) -> None:
    assert input_body["description"] == resumir_texto_livre(DESCRICAO)
    assert "cpf" in input_body["description"]["pii_redigida"]
    assert input_body["address"] == resumir_texto_livre(ENDERECO)
    assert input_body["bairro_raw"] == resumir_texto_livre(BAIRRO)
    assert input_body["latitude"] == {"tipo": "float"}
    assert input_body["longitude"] == {"tipo": "float"}
    assert input_body["alert_id"] == "alerta-sintetico-1"
    assert input_body["alert_type"] == "alagamento"
    assert input_body["severity"] == "baixa"
    assert input_body["environment"] == "staging"


@pytest.mark.asyncio
async def test_falha_ao_gravar_alerta_cor_em_background_nao_envia_o_texto(
    transport, gravacao_que_falha
):
    """Caminho de produção: a tool dispara `save_cor_alert_in_bq_background`,
    que roda `save_cor_alert_in_bq` numa thread do executor; a falha da
    gravação é reportada pelo `@interceptor` dela e engolida pelo wrapper."""
    await bigquery.save_cor_alert_in_bq_background(
        alert_id="alerta-sintetico-1",
        user_id=USER_ID,
        alert_type="alagamento",
        severity="baixa",
        description=DESCRICAO,
        address=ENDERECO,
        latitude=LATITUDE,
        longitude=LONGITUDE,
        timestamp="2026-11-05T12:00:00",
        environment="staging",
        bairro_raw=BAIRRO,
    )

    payload = _payload_enviado(transport)
    _assert_input_body_do_alerta(json.loads(payload["input_body"]))
    assert "falha ao gravar no BigQuery" in payload["error_response"]
    _sem_dado_do_cidadao(payload)


def test_save_cor_alert_in_bq_por_posicao_nao_envia_o_texto(
    transport, gravacao_que_falha
):
    with pytest.raises(RuntimeError, match="falha ao gravar no BigQuery"):
        bigquery.save_cor_alert_in_bq(
            "alerta-sintetico-1",
            USER_ID,
            "alagamento",
            "baixa",
            DESCRICAO,
            ENDERECO,
            LATITUDE,
            LONGITUDE,
            "2026-11-05T12:00:00",
            "staging",
            BAIRRO,
        )

    payload = _payload_enviado(transport)
    _assert_input_body_do_alerta(json.loads(payload["input_body"]))
    _sem_dado_do_cidadao(payload)


def test_save_cor_alert_in_bq_por_nome_sem_bairro(transport, gravacao_que_falha):
    with pytest.raises(RuntimeError):
        bigquery.save_cor_alert_in_bq(
            alert_id="alerta-sintetico-1",
            user_id=USER_ID,
            alert_type="alagamento",
            severity="baixa",
            description=DESCRICAO,
            address=ENDERECO,
            latitude=LATITUDE,
            longitude=LONGITUDE,
            timestamp="2026-11-05T12:00:00",
            environment="staging",
        )

    payload = _payload_enviado(transport)
    input_body = json.loads(payload["input_body"])
    assert input_body["description"] == resumir_texto_livre(DESCRICAO)
    assert input_body["address"] == resumir_texto_livre(ENDERECO)
    assert input_body["latitude"] == {"tipo": "float"}
    assert "bairro_raw" not in input_body
    _sem_dado_do_cidadao(payload)


# ---------------------------------------------------------------------------
# save_response_in_bq
# ---------------------------------------------------------------------------

DATA = {
    "source": "google_search",
    "response": {"query": QUERY, "endereco": ENDERECO},
    "resultados": [QUERY, ENDERECO],
    "total": 2,
}
RESUMO_DE_DATA = {
    "source": {"chars": len("google_search")},
    "response": {"tipo": "dict", "itens": 2},
    "resultados": {"tipo": "list", "itens": 2},
    "total": {"tipo": "int"},
}


@pytest.mark.asyncio
async def test_falha_ao_gravar_resposta_em_background_envia_so_chaves_e_tamanhos(
    transport, gravacao_que_falha
):
    await bigquery.save_response_in_bq_background(
        data=dict(DATA),
        endpoint="/tools/google_search",
        dataset_id="brutos_eai_logs",
        table_id="mcp",
        environment="staging",
    )

    payload = _payload_enviado(transport)
    assert json.loads(payload["input_body"]) == {
        "data": RESUMO_DE_DATA,
        "endpoint": "/tools/google_search",
        "dataset_id": "brutos_eai_logs",
        "table_id": "mcp",
        "project_id": "rj-iplanrio",
        "environment": "staging",
        "use_batch": True,
    }
    assert "falha ao gravar no BigQuery" in payload["error_response"]
    _sem_dado_do_cidadao(payload)


def test_save_response_in_bq_por_nome(transport, gravacao_que_falha):
    with pytest.raises(RuntimeError, match="falha ao gravar no BigQuery"):
        bigquery.save_response_in_bq(
            data=dict(DATA),
            endpoint="/tools/google_search",
            dataset_id="brutos_eai_logs",
            table_id="mcp",
            environment="staging",
        )

    payload = _payload_enviado(transport)
    assert json.loads(payload["input_body"]) == {
        "data": RESUMO_DE_DATA,
        "endpoint": "/tools/google_search",
        "dataset_id": "brutos_eai_logs",
        "table_id": "mcp",
        "environment": "staging",
    }
    _sem_dado_do_cidadao(payload)


def test_save_response_in_bq_com_data_que_nao_e_dict(transport, gravacao_que_falha):
    with pytest.raises(RuntimeError):
        bigquery.save_response_in_bq(
            [QUERY, ENDERECO], "/tools/equipments", "brutos_eai_logs", "mcp"
        )

    payload = _payload_enviado(transport)
    assert json.loads(payload["input_body"]) == {
        "data": {"tipo": "list", "itens": 2},
        "endpoint": "/tools/equipments",
        "dataset_id": "brutos_eai_logs",
        "table_id": "mcp",
    }
    _sem_dado_do_cidadao(payload)


def test_resumo_de_data_corta_nome_longo_e_nao_leva_valor():
    chave_longa = "k" * 200
    resumo = bigquery._input_body_do_save_response(
        ({chave_longa: QUERY, "texto": ENDERECO},), {"endpoint": "/tools/x"}
    )

    assert resumo == {
        "data": {
            "k" * 64: {"chars": len(QUERY)},
            "texto": {"chars": len(ENDERECO)},
        },
        "endpoint": "/tools/x",
    }
