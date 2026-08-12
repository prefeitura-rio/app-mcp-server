import pytest

from src.tools.multi_step_service.workflows.divida_ativa import api_service as module


@pytest.mark.asyncio
async def test_consultar_debitos_usa_contrato_da_tool(monkeypatch):
    calls = []

    async def fake_consultar_debitos(parameters):
        calls.append(parameters)
        return {
            "api_resposta_sucesso": True,
            "mensagem_divida_contribuinte": "mensagem",
        }

    monkeypatch.setattr(module, "consultar_debitos_tool", fake_consultar_debitos)

    service = module.DividaAtivaAPIService(user_id="unit-test-user")
    result = await service.consultar_debitos(
        "auto_infracao",
        "AI-98765",
        ano="2024",
    )

    assert result["api_resposta_sucesso"] is True
    assert calls == [
        {
            "consulta_debitos": "numeroAutoInfracao",
            "numeroAutoInfracao": "98765",
            "anoAutoInfracao": "2024",
        }
    ]


@pytest.mark.asyncio
async def test_emitir_guia_a_vista_usa_contrato_da_tool(monkeypatch):
    calls = []

    async def fake_emitir_guia_a_vista(parameters):
        calls.append(parameters)
        return {
            "api_resposta_sucesso": True,
            "link": "https://example.com/guia.pdf",
            "codigo_de_barras": "123456789",
            "pix": "000201PIX",
        }

    monkeypatch.setattr(module, "emitir_guia_a_vista_tool", fake_emitir_guia_a_vista)

    service = module.DividaAtivaAPIService(user_id="unit-test-user")
    result = await service.emitir_guia_a_vista(cdas=["CDA-1"], efs=["EF-1"])

    assert result["link"] == "https://example.com/guia.pdf"
    assert calls == [
        {
            "itens_informados": ["1", "2"],
            "dicionario_itens": "{'1': 'CDA-1', '2': 'EF-1'}",
            "lista_cdas": "['CDA-1']",
            "lista_efs": "['EF-1']",
            "lista_guias": "[]",
        }
    ]
