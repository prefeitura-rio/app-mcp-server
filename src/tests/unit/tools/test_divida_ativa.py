import importlib.util
import sys
import types
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[4]
MODULE_PATH = PROJECT_ROOT / "src" / "tools" / "divida_ativa.py"


def load_module(module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def divida_module(monkeypatch):
    env_module = types.SimpleNamespace(
        CHATBOT_PGM_API_URL="https://pgm.example.local",
        CHATBOT_PGM_ACCESS_KEY="secret-key",
    )
    logger = types.SimpleNamespace(
        info=lambda *_args, **_kwargs: None, error=lambda *_args, **_kwargs: None
    )
    interceptor_module = types.SimpleNamespace(
        interceptor=lambda *args, **kwargs: lambda func: func
    )

    monkeypatch.setitem(
        sys.modules, "src.config", types.SimpleNamespace(env=env_module)
    )
    monkeypatch.setitem(sys.modules, "src.config.env", env_module)
    monkeypatch.setitem(
        sys.modules, "src.utils.log", types.SimpleNamespace(logger=logger)
    )
    monkeypatch.setitem(sys.modules, "src.utils.error_interceptor", interceptor_module)
    monkeypatch.setitem(
        sys.modules,
        "src.tools.utils",
        types.SimpleNamespace(internal_request=None),
    )

    return load_module("test_divida_ativa_module")


@pytest.mark.asyncio
async def test_pgm_api_success_and_error_paths(divida_module, monkeypatch):
    calls = []

    async def fake_internal_request(url, method, request_kwargs):
        calls.append((url, method, request_kwargs))
        if url.endswith("/security/token"):
            return {"access_token": "token-123"}
        return {"success": True, "data": {"ok": True}}

    monkeypatch.setattr(divida_module, "internal_request", fake_internal_request)

    result = await divida_module.pgm_api(
        endpoint="v2/teste",
        consumidor="consumidor-x",
        data={"cpfCnpj": "123"},
    )

    assert result == {"ok": True}
    assert calls[0][0].endswith("/security/token")
    assert calls[1][2]["headers"]["Authorization"] == "Bearer token-123"

    async def fake_missing_token(url, method, request_kwargs):
        return {"error": "invalid"}

    monkeypatch.setattr(divida_module, "internal_request", fake_missing_token)
    with pytest.raises(Exception, match="Failed to get PGM access token"):
        await divida_module.pgm_api(endpoint="v2/teste", consumidor="x", data={})

    async def fake_business_error(url, method, request_kwargs):
        if url.endswith("/security/token"):
            return {"access_token": "token-123"}
        return {
            "success": False,
            "data": [{"value": "Erro A"}, {"value": "Erro A"}, {"value": "Erro B"}],
        }

    monkeypatch.setattr(divida_module, "internal_request", fake_business_error)
    result = await divida_module.pgm_api(endpoint="v2/teste", consumidor="x", data={})
    assert result["erro"] is True
    assert "Erro A" in result["motivos"]
    assert "Erro B" in result["motivos"]


@pytest.mark.asyncio
async def test_pgm_api_none_timeout_and_timeout_message(divida_module, monkeypatch):
    async def fake_none_response(url, method, request_kwargs):
        if url.endswith("/security/token"):
            return {"access_token": "token-123"}
        return None

    monkeypatch.setattr(divida_module, "internal_request", fake_none_response)
    result = await divida_module.pgm_api(endpoint="v2/teste", consumidor="x", data={})
    assert result == {"success": True}

    async def fake_timeout(url, method, request_kwargs):
        raise TimeoutError("boom")

    monkeypatch.setattr(divida_module, "internal_request", fake_timeout)
    result = await divida_module.pgm_api(endpoint="v2/teste", consumidor="x", data={})
    assert result["erro"] is True
    assert "temporariamente indisponível" in result["motivos"]

    async def fake_timeout_string(url, method, request_kwargs):
        raise Exception("request timeout while calling api")

    monkeypatch.setattr(divida_module, "internal_request", fake_timeout_string)
    result = await divida_module.pgm_api(endpoint="v2/teste", consumidor="x", data={})
    assert result["erro"] is True
    assert "temporariamente indisponível" in result["motivos"]


@pytest.mark.asyncio
async def test_da_emitir_guia_and_processar_registros(divida_module, monkeypatch):
    entrada = await divida_module.da_emitir_guia(
        {
            "itens_informados": '["1","2"]',
            "lista_cdas": '["CDA-1"]',
            "lista_efs": '["EF-2"]',
            "lista_guias": "[]",
            "dicionario_itens": '{"1":"CDA-1","2":"EF-2"}',
        },
        tipo="a_vista",
    )
    assert entrada == {"origem_solicitação": 0, "cdas": ["CDA-1"], "efs": ["EF-2"]}

    entrada = await divida_module.da_emitir_guia(
        {
            "apenas_um_item": "1",
            "lista_cdas": "",
            "lista_efs": "",
            "lista_guias": '["GUIA-1"]',
            "dicionario_itens": '{"1":"GUIA-1"}',
        },
        tipo="regularizacao",
    )
    assert entrada == {"origem_solicitação": 0, "guias": ["GUIA-1"]}

    with pytest.raises(ValueError):
        await divida_module.da_emitir_guia(
            {"itens_informados": "abc", "dicionario_itens": "not-a-dict"},
            tipo="a_vista",
        )

    async def fake_pgm_api(endpoint, consumidor, data):
        return [
            {
                "codigoDeBarras": "123",
                "pdf": "https://example.com/guia.pdf",
                "dataVencimento": "10/04/2026",
                "codigoQrEMVPix": "pix-code",
            }
        ]

    monkeypatch.setattr(divida_module, "pgm_api", fake_pgm_api)
    result = await divida_module.processar_registros(
        endpoint="v2/guias",
        consumidor="emitir",
        parametros_entrada={"origem_solicitação": 0},
    )
    assert result["api_resposta_sucesso"] is True
    assert result["codigo_de_barras"] == "123"
    assert result["pix"] == "pix-code"

    async def fake_pgm_api_error(endpoint, consumidor, data):
        return {"erro": True, "motivos": "Falhou"}

    monkeypatch.setattr(divida_module, "pgm_api", fake_pgm_api_error)
    result = await divida_module.processar_registros(
        endpoint="v2/guias",
        consumidor="emitir",
        parametros_entrada={"origem_solicitação": 0},
    )
    assert result == {"api_resposta_sucesso": False, "api_descricao_erro": "Falhou"}


@pytest.mark.asyncio
async def test_processar_registros_devolve_todas_as_guias(divida_module, monkeypatch):
    """
    CHATR-164: o EPGM emite uma guia por natureza de débito.

    Antes, o loop sobrescrevia os mesmos campos e só a última guia chegava ao
    consumidor — o cidadão pagava uma achando que quitou todas.
    """

    async def fake_pgm_api(endpoint, consumidor, data):
        return [
            {
                "codigoDeBarras": "111",
                "pdf": "a.pdf",
                "dataVencimento": "10/04/2026",
                "codigoQrEMVPix": "pix-1",
            },
            {
                "codigoDeBarras": "222",
                "pdf": "b.pdf",
                "dataVencimento": "11/04/2026",
                "codigoQrEMVPix": "pix-2",
            },
        ]

    monkeypatch.setattr(divida_module, "pgm_api", fake_pgm_api)
    result = await divida_module.processar_registros(
        endpoint="v2/guias",
        consumidor="emitir",
        parametros_entrada={"origem_solicitação": 0},
    )

    assert result["total_guias"] == 2
    assert [guia["pix"] for guia in result["guias_emitidas"]] == ["pix-1", "pix-2"]
    # Campos no topo: a primeira guia, não a última.
    assert result["codigo_de_barras"] == "111"
    assert result["link"] == "a.pdf"


@pytest.mark.asyncio
async def test_processar_registros_ignora_resposta_vazia_da_pgm(
    divida_module, monkeypatch
):
    """`pgm_api` devolve {"success": True} quando a PGM não retorna nada."""

    async def fake_pgm_api(endpoint, consumidor, data):
        return {"success": True}

    monkeypatch.setattr(divida_module, "pgm_api", fake_pgm_api)
    result = await divida_module.processar_registros(
        endpoint="v2/guias",
        consumidor="emitir",
        parametros_entrada={"origem_solicitação": 0},
    )

    assert result["api_resposta_sucesso"] is False
    assert result["api_descricao_erro"] == divida_module.MENSAGEM_SEM_GUIA


@pytest.mark.asyncio
async def test_processar_registros_sem_guia_vira_erro(divida_module, monkeypatch):
    """Sucesso sem guia nenhuma deixaria o cidadão sem nada para pagar."""

    async def fake_pgm_api(endpoint, consumidor, data):
        return []

    monkeypatch.setattr(divida_module, "pgm_api", fake_pgm_api)
    result = await divida_module.processar_registros(
        endpoint="v2/guias",
        consumidor="emitir",
        parametros_entrada={"origem_solicitação": 0},
    )

    assert result["api_resposta_sucesso"] is False
    assert result["api_descricao_erro"] == divida_module.MENSAGEM_SEM_GUIA


@pytest.mark.asyncio
async def test_emitir_guia_wrappers_and_consultar_debitos(divida_module, monkeypatch):
    async def fake_da_emitir_guia(parameters, tipo):
        return {"origem_solicitação": 0, "tipo": tipo}

    async def fake_processar(endpoint, consumidor, parametros_entrada):
        return {
            "api_resposta_sucesso": True,
            "endpoint": endpoint,
            "payload": parametros_entrada,
        }

    monkeypatch.setattr(divida_module, "da_emitir_guia", fake_da_emitir_guia)
    monkeypatch.setattr(divida_module, "processar_registros", fake_processar)

    result = await divida_module.emitir_guia_a_vista({})
    assert result["endpoint"].endswith("/avista")

    result = await divida_module.emitir_guia_regularizacao({})
    assert result["endpoint"].endswith("/regularizacao")

    async def fake_none_entrada(parameters, tipo):
        return None

    monkeypatch.setattr(divida_module, "da_emitir_guia", fake_none_entrada)
    result = await divida_module.emitir_guia_a_vista({})
    assert result["api_resposta_sucesso"] is False

    async def fake_consulta(endpoint, consumidor, data):
        return {
            "enderecoImovel": "Rua X, 10",
            "debitosNaoParceladosComSaldoTotal": {
                "cdasNaoAjuizadasNaoParceladas": [
                    {"cdaId": "CDA-1", "valorSaldoTotal": "R$10"}
                ],
                "efsNaoParceladas": [
                    {
                        "numeroExecucaoFiscal": "EF-1",
                        "saldoExecucaoFiscalNaoParcelada": "R$20",
                    }
                ],
                "saldoTotalNaoParcelado": "R$30",
            },
            "guiasParceladasComSaldoTotal": {
                "guiasParceladas": [
                    {"numero": "GUIA-1", "dataUltimoPagamento": "01/04/2026"}
                ]
            },
            "naturezasDivida": ["IPTU"],
            "dataVencimento": "30/04/2026",
        }

    monkeypatch.setattr(divida_module, "pgm_api", fake_consulta)
    result = await divida_module.consultar_debitos(
        {"consulta_debitos": "inscricaoImobiliaria", "inscricaoImobiliaria": "18.2.3-4"}
    )
    assert result["api_resposta_sucesso"] is True
    assert result["lista_cdas"] == ["CDA-1"]
    assert result["lista_efs"] == ["EF-1"]
    assert result["lista_guias"] == ["GUIA-1"]
    assert result["total_itens_pagamento"] == 3

    result = await divida_module.consultar_debitos(
        {"consulta_debitos": "cpfCnpj", "cpfCnpj": "abc"}
    )
    assert result["api_resposta_sucesso"] is False

    result = await divida_module.consultar_debitos(
        {
            "consulta_debitos": "numeroAutoInfracao",
            "numeroAutoInfracao": "123",
            "anoAutoInfracao": "abc",
        }
    )
    assert result["api_resposta_sucesso"] is False


# Guia real de produção: PIX e código de barras da mesma emissão, ambos
# declarando R$ 4.825,43.
PIX_REAL = (
    "00020101021226850014br.gov.bcb.pix2563pix.santander.com.br/qr/v2/"
    "a2ecf2b0-c305-4a4c-8cb4-40561cff7e0b520400005303986540748"
    "25.435802BR5917PM RIO DE JANEIRO6014RIO DE JANEIRO62070503***63044CD6"
)
BARRAS_REAL = "81650000048-3 25433659202-0 60831418100-9 11098057426-0"
LINK_REAL = (
    "https://daminternet.rio.rj.gov.br//repositoriorelatorioscertidao/"
    "6a13bc0c-f48b-4459-858f-a836d673e210.pdf"
)


@pytest.mark.asyncio
async def test_guia_leva_valor_extraido_do_codigo_de_pagamento(
    divida_module, monkeypatch
):
    """
    CHATR-164: a PGM não devolve o valor da guia.

    A resposta de emissão traz só data, PDF, base64, código de barras e PIX —
    o valor está dentro dos dois últimos e é de lá que sai.
    """

    async def fake_pgm_api(endpoint, consumidor, data):
        return [
            {
                "dataVencimento": "31/08/2026",
                "pdf": LINK_REAL,
                "arquivoBase64": "JVBERi0xLjQK",
                "codigoDeBarras": BARRAS_REAL,
                "codigoQrEMVPix": PIX_REAL,
            }
        ]

    monkeypatch.setattr(divida_module, "pgm_api", fake_pgm_api)
    result = await divida_module.processar_registros(
        endpoint="v2/guiapagamento/emitir/avista",
        consumidor="emitir-guia-vista",
        parametros_entrada={"origem_solicitação": 0, "cdas": ["94/009914/2026-00"]},
    )

    guia = result["guias_emitidas"][0]
    assert guia["valor"] == 4825.43
    # `$id` da PGM é numeração do Json.NET; o GUID do PDF é o que identifica.
    assert guia["id"] == "6a13bc0c-f48b-4459-858f-a836d673e210"


@pytest.mark.asyncio
async def test_guia_sem_valor_apuravel_ainda_e_entregue(divida_module, monkeypatch):
    """
    Guia sem valor extraível continua pagável: omiti-la esconderia do cidadão
    um débito em aberto. O valor vem nulo, e o consumidor decide o que exibir.
    """

    async def fake_pgm_api(endpoint, consumidor, data):
        return [
            {
                "dataVencimento": "31/08/2026",
                "pdf": "sem-guid.pdf",
                "codigoDeBarras": "",
                "codigoQrEMVPix": "nao-e-um-emv",
            }
        ]

    monkeypatch.setattr(divida_module, "pgm_api", fake_pgm_api)
    result = await divida_module.processar_registros(
        endpoint="v2/guias",
        consumidor="emitir",
        parametros_entrada={"origem_solicitação": 0},
    )

    guia = result["guias_emitidas"][0]
    assert result["total_guias"] == 1
    assert guia["valor"] is None
    assert guia["id"] == ""
    assert guia["data_vencimento"] == "31/08/2026"


def test_itens_da_consulta_estrutura_cda_ef_e_guia(divida_module):
    """
    Amostra real de consulta com CDA, EF e guias parceladas.

    Só a CDA traz `naturezaDivida`. A EF tem apenas número e saldo, e a guia
    parcelada vem com `valorTotalGuia` vazio — daí os nulos.
    """
    itens = divida_module._itens_da_consulta(
        cdas=[
            {
                "cdaId": "01/184218/2026-00",
                "naturezaDivida": "IPTU/Taxas - Predial",
                "naturezaDividaGrupoId": "1",
                "valorSaldoPrincipal": "R$1.830,52",
                "valorSaldoHonorarios": "R$91,53",
                "valorSaldoTotal": "R$1.922,05",
            }
        ],
        efs=[
            {
                "numeroExecucaoFiscal": "0334852-76.2017.8.19.0001",
                "saldoExecucaoFiscalNaoParcelada": "R$24.897,81",
            }
        ],
        guias=[{"numero": "2026/0009656", "valorTotalGuia": ""}],
    )

    assert itens == [
        {
            "id": "01/184218/2026-00",
            "tipo": "cda",
            "natureza": "IPTU/Taxas - Predial",
            "natureza_id": "1",
            "valor": 1922.05,
        },
        {
            "id": "0334852-76.2017.8.19.0001",
            "tipo": "ef",
            "natureza": None,
            "natureza_id": None,
            "valor": 24897.81,
        },
        {
            "id": "2026/0009656",
            "tipo": "guia",
            "natureza": None,
            "natureza_id": None,
            "valor": None,
        },
    ]

    # A soma dos itens reproduz o saldo que a mesma resposta informa — é isso
    # que permite ao consumidor casar guia com natureza.
    assert round(itens[0]["valor"] + itens[1]["valor"], 2) == 26819.86


def test_itens_da_consulta_ignora_entrada_que_nao_e_item(divida_module):
    itens = divida_module._itens_da_consulta(
        cdas=["não é dict", None],
        efs=[],
        guias=[],
    )
    assert itens == []
