"""
Testes unitários do workflow de Dívida Ativa — foco no novo nó
_escolher_forma_pagamento e no fluxo completo pós-emissão de guia.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.tools.multi_step_service.core.models import ServiceState
from src.tools.multi_step_service.workflows.divida_ativa.models import (
    OpcaoPagamentoPayload,
)
from src.tools.multi_step_service.workflows.divida_ativa.templates import (
    DividaAtivaTemplates,
)
from src.tools.multi_step_service.workflows.divida_ativa.workflow import (
    DividaAtivaWorkflow,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

RESULTADO_COMPLETO = {
    "api_resposta_sucesso": True,
    "link": "https://example.com/guia.pdf",
    "codigo_de_barras": "1234.5678 9012.3456 7890.1234 5 67890000065000",
    "pix": "00020126580014BR.GOV.BCB.PIX0136abc123",
    "data_vencimento": "30/06/2026",
}

RESULTADO_SEM_PIX = {
    "api_resposta_sucesso": True,
    "link": "https://example.com/guia.pdf",
    "codigo_de_barras": "1234.5678 9012.3456 7890.1234 5 67890000065000",
    "data_vencimento": "30/06/2026",
}

RESULTADO_SO_LINK = {
    "api_resposta_sucesso": True,
    "link": "https://example.com/guia.pdf",
    "data_vencimento": "30/06/2026",
}

CONSULTA_RESULTADO = {
    "api_resposta_sucesso": True,
    "mensagem_divida_contribuinte": "CDA 123 - R$ 100,00",
    "total_nao_parcelado": 1,
    "total_parcelado": 0,
    "dicionario_itens": {1: "123"},
    "lista_cdas": ["123"],
    "lista_efs": [],
    "lista_guias": [],
    "debitos_msg": [{"cda": "123", "valor": "R$ 100,00"}],
}


def make_state(payload=None, data=None):
    return ServiceState(
        user_id="u1",
        service_name="divida_ativa",
        payload=payload or {},
        data=data or {},
    )


def make_workflow():
    wf = DividaAtivaWorkflow.__new__(DividaAtivaWorkflow)
    wf.service_name = "divida_ativa"
    wf.api_service = MagicMock()
    return wf


def make_state_com_guia(payload=None, resultado=None):
    return make_state(
        payload=payload or {},
        data={
            "consulta_debitos": "cda",
            "valor_consulta": "123",
            "consulta_resultado": CONSULTA_RESULTADO,
            "acao": "pagar_a_vista",
            "itens_informados": [1],
            "confirmacao_debitos": True,
            "guia_emitida": resultado or RESULTADO_COMPLETO,
        },
    )


# ---------------------------------------------------------------------------
# Testes: OpcaoPagamentoPayload — validação e fuzzy matching
# ---------------------------------------------------------------------------


def test_opcao_pix_exato():
    assert OpcaoPagamentoPayload(opcao_pagamento="pix").opcao_pagamento == "pix"


def test_opcao_pix_fuzzy():
    p = OpcaoPagamentoPayload.model_validate({"opcao_pagamento": "pix copia e cola"})
    assert p.opcao_pagamento == "pix"


def test_opcao_pix_qrcode():
    p = OpcaoPagamentoPayload.model_validate({"opcao_pagamento": "qr code"})
    assert p.opcao_pagamento == "pix"


def test_opcao_codigo_de_barras_exato():
    p = OpcaoPagamentoPayload(opcao_pagamento="codigo_de_barras")
    assert p.opcao_pagamento == "codigo_de_barras"


def test_opcao_codigo_de_barras_fuzzy():
    p = OpcaoPagamentoPayload.model_validate({"opcao_pagamento": "boleto"})
    assert p.opcao_pagamento == "codigo_de_barras"


def test_opcao_link_pdf():
    p = OpcaoPagamentoPayload.model_validate({"opcao_pagamento": "pdf"})
    assert p.opcao_pagamento == "link"


# ---------------------------------------------------------------------------
# Testes: templates — guia_emitida_escolher_forma
# ---------------------------------------------------------------------------


def test_template_todas_opcoes_disponiveis():
    _, interactive = DividaAtivaTemplates.guia_emitida_escolher_forma(
        RESULTADO_COMPLETO
    )
    ids = [b["id"] for b in interactive["buttons"]]
    assert ids == ["link", "codigo_de_barras", "pix"]


def test_template_sem_pix_apenas_dois_botoes():
    _, interactive = DividaAtivaTemplates.guia_emitida_escolher_forma(RESULTADO_SEM_PIX)
    ids = [b["id"] for b in interactive["buttons"]]
    assert "pix" not in ids
    assert len(ids) == 2


def test_template_so_link_um_botao():
    _, interactive = DividaAtivaTemplates.guia_emitida_escolher_forma(RESULTADO_SO_LINK)
    assert [b["id"] for b in interactive["buttons"]] == ["link"]


def test_template_vencimento_no_texto():
    texto, _ = DividaAtivaTemplates.guia_emitida_escolher_forma(RESULTADO_COMPLETO)
    assert "30/06/2026" in texto


def test_template_field_opcao_pagamento():
    _, interactive = DividaAtivaTemplates.guia_emitida_escolher_forma(
        RESULTADO_COMPLETO
    )
    assert interactive["field"] == "opcao_pagamento"


# ---------------------------------------------------------------------------
# Testes: templates — detalhe_pagamento
# ---------------------------------------------------------------------------


def test_detalhe_link():
    msg = DividaAtivaTemplates.detalhe_pagamento(RESULTADO_COMPLETO, "link")
    assert "https://example.com/guia.pdf" in msg
    assert "30/06/2026" in msg


def test_detalhe_codigo_de_barras():
    msg = DividaAtivaTemplates.detalhe_pagamento(RESULTADO_COMPLETO, "codigo_de_barras")
    assert "1234.5678" in msg
    assert "30/06/2026" in msg


def test_detalhe_pix():
    msg = DividaAtivaTemplates.detalhe_pagamento(RESULTADO_COMPLETO, "pix")
    assert "00020126" in msg
    assert "30/06/2026" in msg


def test_detalhe_sem_vencimento_nao_imprime_campo():
    msg = DividaAtivaTemplates.detalhe_pagamento(
        {"link": "https://x.com/g.pdf"}, "link"
    )
    assert "Data de vencimento" not in msg


# ---------------------------------------------------------------------------
# Testes: nó _escolher_forma_pagamento
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sem_opcao_retorna_botoes():
    wf = make_workflow()
    state = make_state_com_guia()
    await wf._escolher_forma_pagamento(state)

    assert state.agent_response is not None
    ids = [b["id"] for b in state.agent_response.interactive["buttons"]]
    assert "link" in ids
    assert "codigo_de_barras" in ids
    assert "pix" in ids


@pytest.mark.asyncio
async def test_sem_pix_na_api_nao_oferece_botao_pix():
    wf = make_workflow()
    state = make_state_com_guia(resultado=RESULTADO_SEM_PIX)
    await wf._escolher_forma_pagamento(state)

    ids = [b["id"] for b in state.agent_response.interactive["buttons"]]
    assert "pix" not in ids


@pytest.mark.asyncio
async def test_escolhe_pix_retorna_codigo():
    wf = make_workflow()
    state = make_state_com_guia(payload={"opcao_pagamento": "pix"})
    await wf._escolher_forma_pagamento(state)

    assert "00020126" in state.agent_response.description
    assert state.data.get("opcao_pagamento") == "pix"
    assert state.data.get("_reset_on_next_call") is True


@pytest.mark.asyncio
async def test_escolhe_link_retorna_url():
    wf = make_workflow()
    state = make_state_com_guia(payload={"opcao_pagamento": "link"})
    await wf._escolher_forma_pagamento(state)

    assert "https://example.com/guia.pdf" in state.agent_response.description


@pytest.mark.asyncio
async def test_escolhe_codigo_de_barras_retorna_codigo():
    wf = make_workflow()
    state = make_state_com_guia(payload={"opcao_pagamento": "codigo_de_barras"})
    await wf._escolher_forma_pagamento(state)

    assert "1234.5678" in state.agent_response.description


@pytest.mark.asyncio
async def test_opcao_indisponivel_repergunta_sem_reset():
    """Usuário pede PIX mas a API não retornou PIX — deve repergunta sem fechar o fluxo."""
    wf = make_workflow()
    state = make_state_com_guia(
        payload={"opcao_pagamento": "pix"},
        resultado=RESULTADO_SEM_PIX,
    )
    await wf._escolher_forma_pagamento(state)

    assert state.agent_response is not None
    ids = [b["id"] for b in state.agent_response.interactive["buttons"]]
    assert "pix" not in ids
    assert state.data.get("_reset_on_next_call") is not True


@pytest.mark.asyncio
async def test_resposta_final_tem_service_name():
    wf = make_workflow()
    state = make_state_com_guia(payload={"opcao_pagamento": "link"})
    await wf._escolher_forma_pagamento(state)

    assert state.agent_response.service_name == "divida_ativa"


@pytest.mark.asyncio
async def test_fuzzy_pix_copia_cola():
    wf = make_workflow()
    state = make_state_com_guia(payload={"opcao_pagamento": "pix copia e cola"})
    await wf._escolher_forma_pagamento(state)

    assert "00020126" in state.agent_response.description


# ---------------------------------------------------------------------------
# Testes: _emitir_guia não envia resposta final no sucesso
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emitir_guia_sucesso_nao_seta_agent_response():
    wf = make_workflow()
    wf.api_service.emitir_guia = AsyncMock(return_value=RESULTADO_COMPLETO)

    state = make_state(
        data={
            "consulta_resultado": CONSULTA_RESULTADO,
            "acao": "pagar_a_vista",
            "itens_informados": [1],
            "confirmacao_debitos": True,
        }
    )
    await wf._emitir_guia(state)

    assert state.data.get("guia_emitida") == RESULTADO_COMPLETO
    assert state.agent_response is None


@pytest.mark.asyncio
async def test_emitir_guia_erro_api_seta_agent_response():
    wf = make_workflow()
    wf.api_service.emitir_guia = AsyncMock(
        return_value={"api_resposta_sucesso": False, "api_descricao_erro": "Falha X"}
    )

    state = make_state(
        data={
            "consulta_resultado": CONSULTA_RESULTADO,
            "acao": "pagar_a_vista",
            "itens_informados": [1],
            "confirmacao_debitos": True,
        }
    )
    await wf._emitir_guia(state)

    assert state.agent_response is not None
    assert "Falha X" in state.agent_response.description


@pytest.mark.asyncio
async def test_emitir_guia_ja_emitida_nao_rechama_api():
    wf = make_workflow()
    wf.api_service.emitir_guia = AsyncMock()

    state = make_state(
        data={
            "consulta_resultado": CONSULTA_RESULTADO,
            "acao": "pagar_a_vista",
            "itens_informados": [1],
            "confirmacao_debitos": True,
            "guia_emitida": RESULTADO_COMPLETO,
        }
    )
    await wf._emitir_guia(state)

    wf.api_service.emitir_guia.assert_not_called()
    assert state.agent_response is None
