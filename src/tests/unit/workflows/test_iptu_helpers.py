import sys

from src.tools.multi_step_service.core.models import ServiceState

iptu_utils = sys.modules[
    "src.tools.multi_step_service.workflows.iptu_pagamento.helpers.utils"
]
iptu_state_helpers = sys.modules[
    "src.tools.multi_step_service.workflows.iptu_pagamento.helpers.state_helpers"
]
iptu_models = sys.modules[
    "src.tools.multi_step_service.workflows.iptu_pagamento.core.models"
]


class FakeApiService:
    def parse_brazilian_currency(self, value_str: str) -> float:
        return float(value_str.replace(".", "").replace(",", "."))


def _make_cota(
    numero_cota: str,
    esta_paga: bool,
    valor: str,
    vencimento: str,
    esta_vencida: bool = False,
    valor_numerico: float = 0.0,
):
    return iptu_models.Cota(
        Situacao={"codigo": "02"},
        NCota=numero_cota,
        ValorCota=valor,
        DataVencimento=vencimento,
        ValorPago="0,00",
        DataPagamento="",
        QuantDiasEmAtraso="0",
        esta_paga=esta_paga,
        esta_vencida=esta_vencida,
        valor_numerico=valor_numerico,
    )


def test_formatar_valor_brl():
    assert iptu_utils.formatar_valor_brl(None) == "R$ 0,00"
    assert iptu_utils.formatar_valor_brl(1234.56) == "R$ 1.234,56"


def test_preparar_dados_guias_para_template():
    dados = {
        "guias": [
            {
                "numero_guia": "00",
                "tipo": "iptu",
                "valor_iptu_original_guia": "1.234,56",
                "situacao": {"descricao": "EM ABERTO"},
                "esta_em_aberto": True,
            }
        ]
    }

    result = iptu_utils.preparar_dados_guias_para_template(dados, FakeApiService())

    assert result == [
        {
            "numero_guia": "00",
            "tipo": "IPTU",
            "valor_original": 1234.56,
            "situacao": "EM ABERTO",
            "esta_em_aberto": True,
        }
    ]


def test_preparar_dados_cotas_para_template():
    cotas = [
        _make_cota("01", False, "100,00", "01/01/2025", False, 100.0),
        _make_cota("02", True, "200,00", "01/02/2025", False, 200.0),
    ]
    dados = iptu_models.DadosCotas(
        inscricao_imobiliaria="123",
        exercicio="2025",
        numero_guia="00",
        tipo_guia="IPTU",
        cotas=cotas,
    )

    result = iptu_utils.preparar_dados_cotas_para_template(dados)

    assert len(result) == 1
    assert result[0]["numero_cota"] == "01"
    assert result[0]["valor_numerico"] == 100.0


def test_preparar_dados_boletos_para_template_adds_pdf_key():
    guias = [{"tipo": "darm", "numero_guia": "00", "cotas": "01"}]

    result = iptu_utils.preparar_dados_boletos_para_template(guias)

    assert result[0]["pdf"] == "Não disponível"


def test_tem_mais_cotas_disponiveis():
    state = ServiceState(user_id="u1", service_name="iptu_pagamento")
    state.data["dados_cotas"] = {"cotas": [1, 2, 3]}
    state.data["cotas_escolhidas"] = ["1", "2"]

    assert iptu_utils.tem_mais_cotas_disponiveis(state) is True

    state.data["cotas_escolhidas"] = ["1", "2", "3"]
    assert iptu_utils.tem_mais_cotas_disponiveis(state) is False


def test_tem_outras_guias_disponiveis():
    state = ServiceState(user_id="u1", service_name="iptu_pagamento")
    state.data["dados_guias"] = {"guias": [1, 2]}

    assert iptu_utils.tem_outras_guias_disponiveis(state) is True

    state.data["dados_guias"] = {"guias": [1]}
    assert iptu_utils.tem_outras_guias_disponiveis(state) is False


def test_validar_dados_obrigatorios():
    state = ServiceState(user_id="u1", service_name="iptu_pagamento")
    state.data = {"inscricao_imobiliaria": "123", "ano_exercicio": 2025}

    assert (
        iptu_state_helpers.validar_dados_obrigatorios(
            state, ["inscricao_imobiliaria", "ano_exercicio"]
        )
        is None
    )
    assert (
        iptu_state_helpers.validar_dados_obrigatorios(
            state, ["inscricao_imobiliaria", "guia_escolhida"]
        )
        == "guia_escolhida"
    )


def test_reset_para_selecao_cotas():
    state = ServiceState(user_id="u1", service_name="iptu_pagamento")
    state.data = {
        "inscricao_imobiliaria": "123",
        "cotas_escolhidas": ["01"],
        "dados_darm": {"foo": "bar"},
    }
    state.internal = {"darm_separado": True, "dados_confirmados": True}

    iptu_state_helpers.reset_para_selecao_cotas(state)

    assert state.data["inscricao_imobiliaria"] == "123"
    assert "cotas_escolhidas" not in state.data
    assert "dados_darm" not in state.data
    assert "darm_separado" not in state.internal
    assert "dados_confirmados" not in state.internal
