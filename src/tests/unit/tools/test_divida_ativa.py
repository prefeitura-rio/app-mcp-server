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
