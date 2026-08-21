import json
import importlib.util
import sys
import types
from pathlib import Path

import pytest
from pydantic import ValidationError


PROJECT_ROOT = Path(__file__).resolve().parents[4]


def _ensure_package(module_name: str, path: Path):
    pkg = type(sys)(module_name)
    pkg.__path__ = [str(path)]
    sys.modules[module_name] = pkg
    return pkg


def _load_module(module_name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(
        module_name, PROJECT_ROOT / relative_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def divida_ativa_modules(monkeypatch):
    _ensure_package("src", PROJECT_ROOT / "src")
    _ensure_package("src.tools", PROJECT_ROOT / "src" / "tools")
    _ensure_package(
        "src.tools.multi_step_service",
        PROJECT_ROOT / "src" / "tools" / "multi_step_service",
    )
    _ensure_package(
        "src.tools.multi_step_service.core",
        PROJECT_ROOT / "src" / "tools" / "multi_step_service" / "core",
    )
    _ensure_package(
        "src.tools.multi_step_service.workflows",
        PROJECT_ROOT / "src" / "tools" / "multi_step_service" / "workflows",
    )
    _ensure_package(
        "src.tools.multi_step_service.workflows.divida_ativa",
        PROJECT_ROOT
        / "src"
        / "tools"
        / "multi_step_service"
        / "workflows"
        / "divida_ativa",
    )
    _ensure_package(
        "src.tools.multi_step_service.workflows.divida_ativa.core",
        PROJECT_ROOT
        / "src"
        / "tools"
        / "multi_step_service"
        / "workflows"
        / "divida_ativa"
        / "core",
    )

    service_models = _load_module(
        "src.tools.multi_step_service.core.models",
        "src/tools/multi_step_service/core/models.py",
    )
    base_workflow = _load_module(
        "src.tools.multi_step_service.core.base_workflow",
        "src/tools/multi_step_service/core/base_workflow.py",
    )

    monkeypatch.setitem(
        sys.modules,
        "src.tools.multi_step_service.core",
        types.SimpleNamespace(
            AgentResponse=service_models.AgentResponse,
            BaseWorkflow=base_workflow.BaseWorkflow,
            ServiceState=service_models.ServiceState,
            handle_errors=base_workflow.handle_errors,
        ),
    )

    _load_module(
        "src.tools.multi_step_service.workflows.divida_ativa.core.constants",
        "src/tools/multi_step_service/workflows/divida_ativa/core/constants.py",
    )
    models_module = _load_module(
        "src.tools.multi_step_service.workflows.divida_ativa.core.models",
        "src/tools/multi_step_service/workflows/divida_ativa/core/models.py",
    )
    _load_module(
        "src.tools.multi_step_service.workflows.divida_ativa.templates",
        "src/tools/multi_step_service/workflows/divida_ativa/templates.py",
    )
    workflow_module = _load_module(
        "src.tools.multi_step_service.workflows.divida_ativa.divida_ativa_workflow",
        "src/tools/multi_step_service/workflows/divida_ativa/divida_ativa_workflow.py",
    )

    return types.SimpleNamespace(
        ServiceState=service_models.ServiceState,
        DividaAtivaWorkflow=workflow_module.DividaAtivaWorkflow,
        models=models_module,
    )


def _new_state(modules):
    return modules.ServiceState(
        user_id="unit-test-user",
        service_name="divida_ativa",
        status="progress",
        data={},
        internal={},
    )


def _resultado_divida(
    cdas=None,
    efs=None,
    parcelamentos=None,
):
    lista_cdas = [
        getattr(cda, "cda_id", None) or getattr(cda, "numero", None)
        for cda in cdas or []
    ]
    lista_efs = [
        getattr(ef, "numero_execucao_fiscal", None) or getattr(ef, "numero_ef", None)
        for ef in efs or []
    ]
    lista_guias = [getattr(guia, "numero", None) for guia in parcelamentos or []]
    itens = [item for item in [*lista_cdas, *lista_efs, *lista_guias] if item]

    return {
        "api_resposta_sucesso": True,
        "mensagem_divida_contribuinte": (
            "Tipo de consulta:\n"
            "CPF/CNPJ: 12345678901\n\n"
            "*Endereço do imóvel:*\n"
            "Rua A, 123\n\n"
            "Data de Vencimento: 10/09/2026"
        ),
        "lista_cdas": [item for item in lista_cdas if item],
        "lista_efs": [item for item in lista_efs if item],
        "lista_guias": [item for item in lista_guias if item],
        "dicionario_itens": {index: item for index, item in enumerate(itens, start=1)},
        "total_itens_pagamento": len(itens),
        "guias_quantidade_total": len([item for item in lista_guias if item]),
        "efs_cdas_quantidade_total": len(
            [item for item in [*lista_cdas, *lista_efs] if item]
        ),
        "total_nao_parcelado": len(
            [item for item in [*lista_cdas, *lista_efs] if item]
        ),
        "total_parcelado": len([item for item in lista_guias if item]),
        "debitos_msg": [],
    }


class FakeDividaAtivaAPIService:
    def __init__(self, resultado=None, guias=None):
        self.resultado = resultado
        self.guias = guias or {
            "api_resposta_sucesso": True,
            "data_vencimento": "10/09/2026",
            "link": "https://example.com/guia.pdf",
            "codigo_de_barras": "123456789",
            "pix": "000201PIX",
        }
        self.consulta_calls = []
        self.emissao_calls = []

    async def consultar_debitos_por_no(self, node_name, valor, **dados):
        self.consulta_calls.append((node_name, valor, dados))
        return self.resultado

    async def emitir_guia_a_vista(self, cdas, efs):
        self.emissao_calls.append((cdas, efs))
        return self.guias


@pytest.mark.asyncio
async def test_payload_vazio_retorna_schema_do_flow(divida_ativa_modules):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()

    state = await workflow.execute(_new_state(divida_ativa_modules), {})
    response = state.agent_response
    tipo_schema = response.payload_schema["properties"]["tipo_consulta"]

    assert response.service_name == "divida_ativa"
    assert "tipos de consulta" in response.description
    assert tipo_schema["enum"] == [
        "cpf_cnpj",
        "inscricao_imobiliaria",
        "auto_infracao",
        "cda",
        "execucao_fiscal",
    ]
    assert len(tipo_schema["options"]) == 5


def test_cpf_cnpj_rejeita_letras(divida_ativa_modules):
    with pytest.raises(ValidationError):
        divida_ativa_modules.models.CpfCnpjPayload.model_validate(
            {"cpf_cnpj": "abc.def.ghi-jk"}
        )


def test_entrada_rejeita_identificador_sem_numero(divida_ativa_modules):
    with pytest.raises(ValidationError):
        divida_ativa_modules.models.EntradaPayload.model_validate(
            {"entrada": "documento"}
        )


def test_options_de_botoes_nao_incluem_description(divida_ativa_modules):
    schema = divida_ativa_modules.models.OpcaoPagarAVistaPayload.model_json_schema()

    options = schema["properties"]["opcao_pagar_a_vista"]["options"]

    assert options == [
        {"label": "Pagar tudo", "value": "pagar_tudo"},
        {"label": "Escolher os débitos", "value": "escolher_debitos"},
    ]


def test_options_de_listas_nao_incluem_description(divida_ativa_modules):
    schema = (
        divida_ativa_modules.models.MenuPagamentoCompletoPayload.model_json_schema()
    )

    options = schema["properties"]["opcao_menu"]["options"]

    assert schema["properties"]["opcao_menu"]["x-render"] == "list"
    assert options[0] == {"label": "Pagar à vista", "value": "pagar_a_vista"}
    assert all("description" not in option for option in options)


@pytest.mark.asyncio
async def test_tipo_consulta_retorna_lista_explicita(divida_ativa_modules):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()

    state = await workflow.execute(_new_state(divida_ativa_modules), {})

    schema = state.agent_response.payload_schema
    options = schema["properties"]["tipo_consulta"]["options"]
    assert schema["x-render"] == "list"
    assert "tipo_consulta" in schema["properties"]
    assert all("description" not in option for option in options)


@pytest.mark.asyncio
async def test_acao_resultado_retorna_botoes_explicitos(divida_ativa_modules):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_nao_parcelado,
    }

    state = await workflow.execute(state, {"acao_resultado": "outra_acao"})

    assert state.agent_response.payload_schema["x-render"] == "buttons"
    assert "acao_resultado" in state.agent_response.payload_schema["properties"]


@pytest.mark.asyncio
async def test_escolha_multipla_de_debitos_continua_texto_livre(
    divida_ativa_modules,
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_nao_parcelado,
        "lista_cdas": ["CDA-1"],
        "lista_efs": ["EF-1"],
    }

    state = await workflow.execute(
        state,
        {"opcao_pagar_a_vista": "escolher_debitos"},
    )

    schema = state.agent_response.payload_schema
    campo = schema["properties"]["debitos_escolhidos"]
    assert schema.get("x-render") is None
    assert campo.get("enum") is None
    assert campo.get("options") is None


@pytest.mark.asyncio
async def test_tipo_consulta_valido_salva_no_state(divida_ativa_modules):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = await workflow.execute(_new_state(divida_ativa_modules), {})
    state = await workflow.execute(state, {"continuar": True})

    state = await workflow.execute(state, {"tipo_consulta": "cpf_cnpj"})

    assert state.internal["tipo_consulta_cache"] == "cpf_cnpj"
    assert state.data["tipo_consulta"] == "cpf_cnpj"
    assert "cpf_cnpj" in state.agent_response.payload_schema["properties"]
    assert "CPF/CNPJ" in state.agent_response.description


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tipo_consulta", "schema_key", "texto_esperado"),
    [
        ("cpf_cnpj", "cpf_cnpj", "CPF/CNPJ"),
        (
            "inscricao_imobiliaria",
            "inscricao_imobiliaria",
            "Inscrição Imobiliária",
        ),
        ("auto_infracao", "numero_auto_infracao", "Auto de Infração"),
        ("cda", "cda", "CDA"),
        ("execucao_fiscal", "execucao_fiscal", "Execução Fiscal"),
    ],
)
async def test_cada_tipo_consulta_retorna_schema_do_identificador_na_mesma_iteracao(
    divida_ativa_modules, tipo_consulta, schema_key, texto_esperado
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = await workflow.execute(_new_state(divida_ativa_modules), {})
    state = await workflow.execute(state, {"continuar": True})

    state = await workflow.execute(state, {"tipo_consulta": tipo_consulta})

    assert state.internal["tipo_consulta_cache"] == tipo_consulta
    assert schema_key in state.agent_response.payload_schema["properties"]
    assert texto_esperado in state.agent_response.description


@pytest.mark.asyncio
async def test_auto_infracao_retorna_schema_com_ano_e_numero(divida_ativa_modules):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = await workflow.execute(_new_state(divida_ativa_modules), {})
    state = await workflow.execute(state, {"continuar": True})

    state = await workflow.execute(state, {"tipo_consulta": "auto_infracao"})

    properties = state.agent_response.payload_schema["properties"]
    assert "ano_auto_infracao" in properties
    assert "numero_auto_infracao" in properties


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "tipo_consulta",
        "payload",
        "expected_node",
        "expected_value",
        "expected_kwargs",
    ),
    [
        (
            "cpf_cnpj",
            {"cpf_cnpj": "12.345.678/0001-90"},
            "consultar_cpf_cnpj",
            "12.345.678/0001-90",
            {},
        ),
        (
            "inscricao_imobiliaria",
            {"inscricao_imobiliaria": "1234567"},
            "consultar_inscricao_imobiliaria",
            "1234567",
            {},
        ),
        (
            "auto_infracao",
            {"ano_auto_infracao": "2024", "numero_auto_infracao": "98765"},
            "consultar_auto_infracao",
            "98765",
            {"ano": "2024"},
        ),
        (
            "cda",
            {"cda": "202400123"},
            "consultar_cda",
            "202400123",
            {},
        ),
        (
            "execucao_fiscal",
            {"execucao_fiscal": "1234567890"},
            "consultar_execucao_fiscal",
            "1234567890",
            {},
        ),
    ],
)
async def test_payload_do_identificador_sem_debitos_mantem_mesmo_step(
    divida_ativa_modules,
    tipo_consulta,
    payload,
    expected_node,
    expected_value,
    expected_kwargs,
):
    class FakeAPIService:
        def __init__(self):
            self.calls = []

        async def consultar_debitos_por_no(self, node_name, valor, **dados):
            self.calls.append((node_name, valor, dados))
            return None

    api_service = FakeAPIService()
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    workflow._api_service = api_service

    state = await workflow.execute(_new_state(divida_ativa_modules), {})
    state = await workflow.execute(state, {"continuar": True})
    state = await workflow.execute(state, {"tipo_consulta": tipo_consulta})

    state = await workflow.execute(state, payload)

    assert api_service.calls == [(expected_node, expected_value, expected_kwargs)]
    assert state.internal["tipo_consulta_cache"] == tipo_consulta
    assert state.internal["consulta_realizada"] is True
    assert set(payload).issubset(
        state.agent_response.payload_schema["properties"].keys()
    )
    assert "Não encontrei débitos" in state.agent_response.description


@pytest.mark.asyncio
async def test_corrigir_ano_auto_infracao_descarta_consulta_anterior(
    divida_ativa_modules,
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    workflow._api_service = FakeDividaAtivaAPIService(
        resultado=_resultado_divida(
            cdas=[types.SimpleNamespace(cda_id="CDA-1", numero=None)]
        )
    )

    state = await workflow.execute(_new_state(divida_ativa_modules), {})
    state = await workflow.execute(state, {"tipo_consulta": "auto_infracao"})
    state = await workflow.execute(
        state,
        {"ano_auto_infracao": "2024", "numero_auto_infracao": "98765"},
    )

    assert "divida_ativa" in state.data
    assert state.data["ano_auto_infracao"] == "2024"
    assert state.data["numero_auto_infracao"] == "98765"

    state = await workflow.execute(state, {"ano_auto_infracao": "2025"})

    assert "divida_ativa" not in state.data
    assert "numero_auto_infracao" not in state.data
    assert state.data["ano_auto_infracao"] == "2025"
    assert "ano_auto_infracao" in state.agent_response.payload_schema["properties"]
    assert "numero_auto_infracao" in state.agent_response.payload_schema["properties"]
    assert state.agent_response.data["proximo_payload"]["campos"] == [
        "ano_auto_infracao",
        "numero_auto_infracao",
    ]


@pytest.mark.asyncio
async def test_reconsulta_identica_cpf_cnpj_reusa_cache_sem_chamar_api(
    divida_ativa_modules,
):
    resultado = _resultado_divida(
        cdas=[types.SimpleNamespace(cda_id="CDA-1", numero=None)],
    )
    api_service = FakeDividaAtivaAPIService(resultado=resultado)
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    workflow._api_service = api_service

    state = await workflow.execute(_new_state(divida_ativa_modules), {})
    state = await workflow.execute(state, {"tipo_consulta": "cpf_cnpj"})
    state = await workflow.execute(state, {"cpf_cnpj": "12345678901"})

    assert api_service.consulta_calls == [("consultar_cpf_cnpj", "12345678901", {})]

    state = await workflow.execute(state, {"cpf_cnpj": "12345678901"})

    assert api_service.consulta_calls == [("consultar_cpf_cnpj", "12345678901", {})]
    assert "acao_resultado" in state.agent_response.payload_schema["properties"]
    assert "Tipo de consulta:" in state.agent_response.description


@pytest.mark.asyncio
async def test_reconsulta_identica_auto_infracao_reusa_cache_sem_chamar_api(
    divida_ativa_modules,
):
    resultado = _resultado_divida(
        cdas=[types.SimpleNamespace(cda_id="CDA-1", numero=None)],
    )
    api_service = FakeDividaAtivaAPIService(resultado=resultado)
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    workflow._api_service = api_service

    state = await workflow.execute(_new_state(divida_ativa_modules), {})
    state = await workflow.execute(state, {"tipo_consulta": "auto_infracao"})
    state = await workflow.execute(
        state,
        {"ano_auto_infracao": "2024", "numero_auto_infracao": "98765"},
    )

    assert api_service.consulta_calls == [
        ("consultar_auto_infracao", "98765", {"ano": "2024"})
    ]

    state = await workflow.execute(
        state,
        {"ano_auto_infracao": "2024", "numero_auto_infracao": "98765"},
    )

    assert api_service.consulta_calls == [
        ("consultar_auto_infracao", "98765", {"ano": "2024"})
    ]
    assert "acao_resultado" in state.agent_response.payload_schema["properties"]


@pytest.mark.asyncio
async def test_tipo_consulta_tem_precedencia_sobre_reconsulta_cacheada(
    divida_ativa_modules,
):
    resultado = _resultado_divida(
        cdas=[types.SimpleNamespace(cda_id="CDA-1", numero=None)],
    )
    api_service = FakeDividaAtivaAPIService(resultado=resultado)
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    workflow._api_service = api_service

    state = await workflow.execute(_new_state(divida_ativa_modules), {})
    state = await workflow.execute(state, {"tipo_consulta": "cpf_cnpj"})
    state = await workflow.execute(state, {"cpf_cnpj": "12345678901"})

    state = await workflow.execute(
        state,
        {"tipo_consulta": "cda", "cpf_cnpj": "12345678901"},
    )

    assert state.data["tipo_consulta"] == "cda"
    assert "divida_ativa" not in state.data
    assert "cda" in state.agent_response.payload_schema["properties"]
    assert api_service.consulta_calls == [("consultar_cpf_cnpj", "12345678901", {})]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "cdas",
        "efs",
        "parcelamentos",
        "opcoes_menu",
    ),
    [
        (
            [types.SimpleNamespace(cda_id="CDA-1", numero=None)],
            [],
            [types.SimpleNamespace(numero="GUIA-1")],
            [
                "pagar_a_vista",
                "parcelar_debitos",
                "regularizar_debitos",
                "liquidar_parcelamento",
                "emitir_2_via",
                "voltar",
            ],
        ),
        (
            [types.SimpleNamespace(cda_id="CDA-1", numero=None)],
            [types.SimpleNamespace(numero_execucao_fiscal="EF-1", numero_ef=None)],
            [],
            ["pagar_a_vista", "parcelar_debitos", "voltar"],
        ),
        (
            [],
            [],
            [types.SimpleNamespace(numero="GUIA-1")],
            [
                "parcelar_debitos",
                "regularizar_debitos",
                "liquidar_parcelamento",
                "emitir_2_via",
                "voltar",
            ],
        ),
    ],
)
async def test_resultado_da_api_formata_mensagem_e_opcoes_menu(
    divida_ativa_modules,
    cdas,
    efs,
    parcelamentos,
    opcoes_menu,
):
    resultado_api = _resultado_divida(
        cdas=cdas,
        efs=efs,
        parcelamentos=parcelamentos,
    )

    class FakeAPIService:
        async def consultar_debitos_por_no(self, node_name, valor, **dados):
            return resultado_api

    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    workflow._api_service = FakeAPIService()

    state = await workflow.execute(_new_state(divida_ativa_modules), {})
    state = await workflow.execute(state, {"continuar": True})
    state = await workflow.execute(state, {"tipo_consulta": "inscricao_imobiliaria"})
    state = await workflow.execute(state, {"inscricao_imobiliaria": "1234567"})

    divida_ativa = state.data["divida_ativa"]
    assert divida_ativa["opcoes_menu"] == opcoes_menu
    assert divida_ativa["total_nao_parcelado"] == len(cdas) + len(efs)
    assert divida_ativa["total_parcelado"] == len(parcelamentos)
    assert "Tipo de consulta:" in state.agent_response.description
    assert "*Endereço do imóvel:*" in state.agent_response.description
    assert "Data de Vencimento: 10/09/2026" in state.agent_response.description
    assert "acao_resultado" in state.agent_response.payload_schema["properties"]


@pytest.mark.asyncio
async def test_agent_response_data_eh_compacto_apos_consulta(
    divida_ativa_modules,
):
    resultado_api = _resultado_divida(
        cdas=[
            types.SimpleNamespace(cda_id=f"CDA-{index}", numero=None)
            for index in range(1, 60)
        ],
        efs=[
            types.SimpleNamespace(
                numero_execucao_fiscal=f"EF-{index}",
                numero_ef=None,
            )
            for index in range(1, 60)
        ],
    )
    resultado_api["debitos_msg"] = ["linha de débito " * 100 for _ in range(100)]
    resultado_api["url_pdf"] = "https://example.com/consulta.pdf"

    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    workflow._api_service = FakeDividaAtivaAPIService(resultado=resultado_api)

    state = await workflow.execute(_new_state(divida_ativa_modules), {})
    state = await workflow.execute(state, {"tipo_consulta": "cpf_cnpj"})
    state = await workflow.execute(state, {"cpf_cnpj": "12345678901"})

    public_data = state.agent_response.data
    public_json = json.dumps(public_data, ensure_ascii=False)
    cache_json = json.dumps(state.internal["divida_ativa_cache"], ensure_ascii=False)

    assert "dicionario_itens" not in state.data["divida_ativa"]
    assert "dicionario_itens" in state.internal["divida_ativa_cache"]
    assert "divida_ativa" not in public_data
    assert "cpf_cnpj" not in public_data
    assert "tipo_consulta" not in public_data
    assert "dicionario_itens" not in public_json
    assert "debitos_msg" not in public_json
    assert "mensagem_divida_contribuinte" not in public_json
    assert len(public_json) < len(cache_json) / 4
    assert public_data["proximo_payload"]["campos"] == ["acao_resultado"]
    assert public_data["proximo_payload"]["renderizacao"] == "buttons"


@pytest.mark.asyncio
async def test_agent_response_data_explica_voltar_e_corrigir(
    divida_ativa_modules,
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["tipo_consulta"] = "cpf_cnpj"
    state.data["cpf_cnpj"] = "12345678901"
    state.data["divida_ativa"] = {
        "tipo_consulta": "cpf_cnpj",
        "valor_consulta": "12345678901",
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_completo,
    }

    state = await workflow.execute(state, {"acao_resultado": "pagar_agora"})

    data = state.agent_response.data
    assert data["proximo_payload"]["campo_principal"] == "opcao_menu"
    assert data["proximo_payload"]["renderizacao"] == "list"
    assert data["navegacao"]["pode_voltar"] is True
    assert data["navegacao"]["payload_voltar"] == {"opcao_menu": "voltar"}
    assert data["navegacao"]["volta_para"] == "acao_resultado"
    assert any(
        item["campos"] == ["tipo_consulta"] for item in data["navegacao"]["corrigir"]
    )
    assert any(item["campos"] == ["cpf_cnpj"] for item in data["navegacao"]["corrigir"])


@pytest.mark.asyncio
async def test_agent_response_data_explica_corrigir_auto_infracao(
    divida_ativa_modules,
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["tipo_consulta"] = "auto_infracao"
    state.data["ano_auto_infracao"] = "2024"
    state.data["numero_auto_infracao"] = "98765"
    state.data["divida_ativa"] = {
        "tipo_consulta": "auto_infracao",
        "valor_consulta": "98765 2024",
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_nao_parcelado,
    }

    state = await workflow.execute(state, {"acao_resultado": "pagar_agora"})

    assert any(
        item["campos"] == ["ano_auto_infracao", "numero_auto_infracao"]
        for item in state.agent_response.data["navegacao"]["corrigir"]
    )


@pytest.mark.asyncio
async def test_pagar_agora_retorna_menu_conforme_opcoes_menu(divida_ativa_modules):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_completo,
    }

    state = await workflow.execute(state, {"acao_resultado": "pagar_agora"})

    schema = state.agent_response.payload_schema
    opcao_schema = schema["properties"]["opcao_menu"]
    assert schema["x-render"] == "list"
    assert opcao_schema["enum"] == workflow.opcoes_menu_completo
    assert opcao_schema["options"][0]["label"] == "Pagar à vista"
    assert all("description" not in option for option in opcao_schema["options"])
    assert state.data["divida_ativa"]["renderizacao_menu"] == "list"


@pytest.mark.asyncio
async def test_pagar_agora_parcelado_retorna_lista_pydantic(divida_ativa_modules):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_parcelado,
    }

    state = await workflow.execute(state, {"acao_resultado": "pagar_agora"})

    schema = state.agent_response.payload_schema
    opcao_schema = schema["properties"]["opcao_menu"]
    assert schema["x-render"] == "list"
    assert opcao_schema["enum"] == workflow.opcoes_menu_parcelado
    assert opcao_schema["options"][0]["label"] == "Parcelar débitos"
    assert all("description" not in option for option in opcao_schema["options"])
    assert state.data["divida_ativa"]["renderizacao_menu"] == "list"


@pytest.mark.asyncio
async def test_pagar_agora_nao_parcelado_retorna_botoes(divida_ativa_modules):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_nao_parcelado,
    }

    state = await workflow.execute(state, {"acao_resultado": "pagar_agora"})

    schema = state.agent_response.payload_schema
    assert schema["x-render"] == "buttons"
    assert (
        schema["properties"]["opcao_menu"]["enum"] == workflow.opcoes_menu_nao_parcelado
    )
    assert state.data["divida_ativa"]["renderizacao_menu"] == "buttons"


@pytest.mark.asyncio
async def test_consultar_outro_debito_limpa_divida_e_volta_para_tipo(
    divida_ativa_modules,
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_completo,
    }

    state = await workflow.execute(
        state,
        {"acao_resultado": "consultar_outro_debito"},
    )

    assert "divida_ativa" not in state.data
    assert "consulta_realizada" not in state.internal
    assert "tipo_consulta" in state.agent_response.payload_schema["properties"]


@pytest.mark.asyncio
async def test_acao_resultado_invalida_retorna_botoes(
    divida_ativa_modules,
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_completo,
    }

    state = await workflow.execute(state, {"acao_resultado": "outra_acao"})

    assert "Essa opção não existe" in state.agent_response.description
    assert "acao_resultado" in state.agent_response.payload_schema["properties"]


@pytest.mark.asyncio
async def test_opcao_menu_voltar_descarta_ultima_opcao_e_volta_para_resultado(
    divida_ativa_modules,
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["tipo_consulta"] = "cpf_cnpj"
    state.data["cpf_cnpj"] = "12345678901"
    state.data["acao_resultado"] = "pagar_agora"
    state.data["opcao_menu"] = "voltar"
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_completo,
        "renderizacao_menu": "list",
        "opcao_menu_selecionada": "voltar",
    }

    state = await workflow.execute(state, {"opcao_menu": "voltar"})

    assert state.data["tipo_consulta"] == "cpf_cnpj"
    assert state.data["cpf_cnpj"] == "12345678901"
    assert state.data["divida_ativa"]["mensagem_divida_contribuinte"] == "mensagem"
    assert state.internal["consulta_realizada"] is True
    assert "acao_resultado" not in state.data
    assert "opcao_menu" not in state.data
    assert "renderizacao_menu" not in state.data["divida_ativa"]
    assert "opcao_menu_selecionada" not in state.data["divida_ativa"]
    assert "tipo_consulta_cache" not in state.internal
    assert state.internal["current_view"] == "acao_resultado"
    assert "mensagem" in state.agent_response.description
    assert "acao_resultado" in state.agent_response.payload_schema["properties"]


@pytest.mark.asyncio
async def test_opcao_menu_antiga_apos_voltar_nao_dispara_parcelamento(
    divida_ativa_modules,
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["tipo_consulta"] = "cpf_cnpj"
    state.data["cpf_cnpj"] = "12345678901"
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_parcelado,
    }

    state = await workflow.execute(state, {"acao_resultado": "pagar_agora"})
    assert "opcao_menu" in state.agent_response.payload_schema["properties"]

    state = await workflow.execute(state, {"opcao_menu": "voltar"})
    assert "acao_resultado" in state.agent_response.payload_schema["properties"]

    state = await workflow.execute(state, {"opcao_menu": "parcelar_debitos"})

    assert "parcelamento-em-divida-ativa" not in state.agent_response.description
    assert "Essa opção não existe" in state.agent_response.description
    assert "acao_resultado" in state.agent_response.payload_schema["properties"]


@pytest.mark.asyncio
async def test_voltar_usa_view_atual_para_retornar_ao_menu_pagamento(
    divida_ativa_modules,
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["tipo_consulta"] = "cpf_cnpj"
    state.data["cpf_cnpj"] = "12345678901"
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_nao_parcelado,
        "lista_cdas": ["CDA-1"],
        "lista_efs": [],
    }

    state = await workflow.execute(state, {"acao_resultado": "pagar_agora"})
    state = await workflow.execute(state, {"opcao_menu": "pagar_a_vista"})
    assert state.internal["current_view"] == "opcao_pagar_a_vista"

    state = await workflow.execute(state, {"botao": "voltar"})

    assert "Como você quer seguir com o pagamento?" in state.agent_response.description
    assert "opcao_menu" in state.agent_response.payload_schema["properties"]
    assert state.internal["current_view"] == "opcao_menu"
    assert "opcao_menu" not in state.data
    assert "opcao_pagar_a_vista" not in state.data
    assert "opcao_menu_selecionada" not in state.data["divida_ativa"]


@pytest.mark.asyncio
async def test_opcao_menu_indisponivel_retorna_menu_atual(divida_ativa_modules):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_nao_parcelado,
    }

    state = await workflow.execute(state, {"opcao_menu": "regularizar_debitos"})

    assert "Essa opção não está disponível" in state.agent_response.description
    assert state.agent_response.error_message
    assert (
        state.agent_response.payload_schema["properties"]["opcao_menu"]["enum"]
        == workflow.opcoes_menu_nao_parcelado
    )


@pytest.mark.asyncio
async def test_opcao_menu_pagar_a_vista_retorna_botoes(divida_ativa_modules):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_nao_parcelado,
    }

    state = await workflow.execute(state, {"opcao_menu": "pagar_a_vista"})

    schema = state.agent_response.payload_schema
    assert "pagamento à vista" in state.agent_response.description
    assert schema["x-render"] == "buttons"
    assert schema["properties"]["opcao_pagar_a_vista"]["enum"] == [
        "pagar_tudo",
        "escolher_debitos",
    ]
    assert state.data["divida_ativa"]["opcao_menu_selecionada"] == "pagar_a_vista"


@pytest.mark.asyncio
async def test_opcao_pagar_a_vista_invalida_retorna_botoes(
    divida_ativa_modules,
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_nao_parcelado,
        "lista_cdas": ["CDA-1"],
        "lista_efs": [],
        "debitos_pagamento_a_vista": [
            {"tipo": "cda", "identificador": "CDA-1", "label": "1. CDA-1"},
        ],
    }

    state = await workflow.execute(
        state,
        {"opcao_pagar_a_vista": "qualquer_coisa"},
    )

    assert "Essa opção não existe" in state.agent_response.description
    assert state.agent_response.payload_schema["properties"]["opcao_pagar_a_vista"][
        "enum"
    ] == ["pagar_tudo", "escolher_debitos"]


@pytest.mark.asyncio
async def test_pagar_tudo_retorna_confirmacao_com_debitos(
    divida_ativa_modules,
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_nao_parcelado,
        "lista_cdas": ["CDA-1"],
        "lista_efs": ["EF-1"],
    }

    state = await workflow.execute(state, {"opcao_pagar_a_vista": "pagar_tudo"})

    assert "Os débitos escolhidos foram:" in state.agent_response.description
    assert "1. CDA-1" in state.agent_response.description
    assert "2. EF-1" in state.agent_response.description
    assert (
        "confirmar_pagamento_a_vista"
        in state.agent_response.payload_schema["properties"]
    )
    assert state.data["divida_ativa"]["debitos_pagamento_a_vista_labels"] == [
        "1. CDA-1",
        "2. EF-1",
    ]


@pytest.mark.asyncio
async def test_escolher_debitos_retorna_schema_de_input(
    divida_ativa_modules,
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_nao_parcelado,
        "lista_cdas": ["CDA-1"],
        "lista_efs": ["EF-1"],
    }

    state = await workflow.execute(
        state,
        {"opcao_pagar_a_vista": "escolher_debitos"},
    )

    assert "numerados de 1 a 2" in state.agent_response.description
    assert "debitos_escolhidos" in state.agent_response.payload_schema["properties"]


@pytest.mark.asyncio
async def test_debitos_escolhidos_validos_retorna_confirmacao(
    divida_ativa_modules,
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_nao_parcelado,
        "lista_cdas": ["CDA-1", "CDA-2"],
        "lista_efs": ["EF-1"],
    }

    state = await workflow.execute(state, {"debitos_escolhidos": "1, 3"})

    assert "1. CDA-1" in state.agent_response.description
    assert "3. EF-1" in state.agent_response.description
    assert "2. CDA-2" not in state.agent_response.description
    assert (
        "confirmar_pagamento_a_vista"
        in state.agent_response.payload_schema["properties"]
    )
    assert state.data["divida_ativa"]["debitos_pagamento_a_vista_labels"] == [
        "1. CDA-1",
        "3. EF-1",
    ]


@pytest.mark.asyncio
async def test_debitos_escolhidos_invalidos_retorna_schema(
    divida_ativa_modules,
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_nao_parcelado,
        "lista_cdas": ["CDA-1"],
        "lista_efs": [],
        "debitos_pagamento_a_vista": [
            {"tipo": "cda", "identificador": "CDA-1", "label": "1. CDA-1"},
        ],
    }

    state = await workflow.execute(state, {"debitos_escolhidos": "2"})

    assert "Não consegui identificar esses débitos" in state.agent_response.description
    assert "debitos_escolhidos" in state.agent_response.payload_schema["properties"]


@pytest.mark.asyncio
async def test_confirmacao_pagamento_invalida_retorna_botoes(
    divida_ativa_modules,
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_nao_parcelado,
        "lista_cdas": ["CDA-1"],
        "lista_efs": [],
        "debitos_pagamento_a_vista": [
            {"tipo": "cda", "identificador": "CDA-1", "label": "1. CDA-1"},
        ],
    }

    state = await workflow.execute(
        state,
        {"confirmar_pagamento_a_vista": "talvez"},
    )

    assert "Essa opção não existe" in state.agent_response.description
    assert state.agent_response.payload_schema["properties"][
        "confirmar_pagamento_a_vista"
    ]["enum"] == ["sim", "nao"]


@pytest.mark.asyncio
async def test_confirmacao_pagamento_nao_retorna_menu_de_retentativa(
    divida_ativa_modules,
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_nao_parcelado,
        "lista_cdas": ["CDA-1"],
        "lista_efs": [],
        "debitos_pagamento_a_vista": [
            {"tipo": "cda", "identificador": "CDA-1", "label": "1. CDA-1"},
        ],
    }

    state = await workflow.execute(
        state,
        {"confirmar_pagamento_a_vista": "nao"},
    )

    assert "Tudo bem. Vamos tentar novamente." in state.agent_response.description
    assert (
        "acao_pagamento_recusado" in state.agent_response.payload_schema["properties"]
    )
    assert state.agent_response.payload_schema["properties"]["acao_pagamento_recusado"][
        "enum"
    ] == ["escolher_debitos", "opcoes_pagamento", "encerrar_atendimento"]


@pytest.mark.asyncio
async def test_confirmacao_pagamento_sim_retorna_formas_de_pagamento(
    divida_ativa_modules,
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_nao_parcelado,
        "lista_cdas": ["CDA-1"],
        "lista_efs": [],
        "debitos_pagamento_a_vista": [
            {"tipo": "cda", "identificador": "CDA-1", "label": "1. CDA-1"},
        ],
    }

    state = await workflow.execute(
        state,
        {"confirmar_pagamento_a_vista": "sim"},
    )

    assert "Agora escolha uma das três opções" in state.agent_response.description
    assert state.agent_response.payload_schema["properties"]["forma_pagamento_a_vista"][
        "enum"
    ] == ["boleto_bancario", "codigo_barras", "pix_copia_e_cola"]


@pytest.mark.asyncio
async def test_forma_pagamento_invalida_retorna_botoes(
    divida_ativa_modules,
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_nao_parcelado,
        "debitos_pagamento_a_vista": [
            {"tipo": "cda", "identificador": "CDA-1", "label": "1. CDA-1"},
        ],
    }

    state = await workflow.execute(
        state,
        {"forma_pagamento_a_vista": "dinheiro"},
    )

    assert "Essa opção não existe" in state.agent_response.description
    assert (
        "forma_pagamento_a_vista" in state.agent_response.payload_schema["properties"]
    )


@pytest.mark.asyncio
async def test_forma_pagamento_sem_debitos_selecionados_nao_emite_guia(
    divida_ativa_modules,
):
    class FakeAPIService:
        async def emitir_guia_a_vista(self, cdas, efs):
            raise AssertionError("não deveria emitir guia sem débitos escolhidos")

    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    workflow._api_service = FakeAPIService()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_nao_parcelado,
        "lista_cdas": ["CDA-1"],
        "lista_efs": [],
    }

    state = await workflow.execute(
        state,
        {"forma_pagamento_a_vista": "pix_copia_e_cola"},
    )

    assert "Não consegui entender esse input" in state.agent_response.description
    assert "opcao_pagar_a_vista" in state.agent_response.payload_schema["properties"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("forma_pagamento", "texto_esperado"),
    [
        ("boleto_bancario", "https://example.com/guia.pdf"),
        ("codigo_barras", "123456789"),
        ("pix_copia_e_cola", "000201PIX"),
    ],
)
async def test_forma_pagamento_valida_salva_e_responde(
    divida_ativa_modules,
    forma_pagamento,
    texto_esperado,
):
    class FakeAPIService:
        def __init__(self):
            self.calls = []

        async def emitir_guia_a_vista(self, cdas, efs):
            self.calls.append((cdas, efs))
            return {
                "api_resposta_sucesso": True,
                "data_vencimento": "10/09/2026",
                "link": "https://example.com/guia.pdf",
                "codigo_de_barras": "123456789",
                "pix": "000201PIX",
            }

    api_service = FakeAPIService()
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    workflow._api_service = api_service
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_nao_parcelado,
        "debitos_pagamento_a_vista": [
            {"tipo": "cda", "identificador": "CDA-1"},
            {"tipo": "execucao_fiscal", "identificador": "EF-1"},
        ],
    }

    state = await workflow.execute(
        state,
        {"forma_pagamento_a_vista": forma_pagamento},
    )

    assert "Beleza." in state.agent_response.description
    assert texto_esperado in state.agent_response.description
    assert state.agent_response.payload_schema is None
    assert state.status == "completed"
    assert state.data == {}
    assert state.internal == {}
    guia_publica = {
        "data_vencimento": "10/09/2026",
        "link": "https://example.com/guia.pdf",
        "codigo_de_barras": "123456789",
        "pix": "000201PIX",
    }
    assert state.agent_response.data == {
        "status": "completed",
        "guia_pagamento_a_vista": {
            **guia_publica,
            "guias": [guia_publica],
            "total_guias": 1,
        },
    }
    assert api_service.calls == [(["CDA-1"], ["EF-1"])]


@pytest.mark.asyncio
async def test_forma_pagamento_com_multiplas_guias_entrega_todas(
    divida_ativa_modules,
):
    """CHATR-164: o EPGM emite uma guia por natureza de débito."""

    class FakeAPIService:
        async def emitir_guia_a_vista(self, cdas, efs):
            return {
                "api_resposta_sucesso": True,
                "total_guias": 2,
                "guias_emitidas": [
                    {
                        "data_vencimento": "10/09/2026",
                        "link": "https://example.com/guia-1.pdf",
                        "codigo_de_barras": "111",
                        "pix": "PIX-1",
                    },
                    {
                        "data_vencimento": "11/09/2026",
                        "link": "https://example.com/guia-2.pdf",
                        "codigo_de_barras": "222",
                        "pix": "PIX-2",
                    },
                ],
                "data_vencimento": "10/09/2026",
                "link": "https://example.com/guia-1.pdf",
                "codigo_de_barras": "111",
                "pix": "PIX-1",
            }

    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    workflow._api_service = FakeAPIService()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_nao_parcelado,
        "debitos_pagamento_a_vista": [
            {"tipo": "cda", "identificador": "CDA-1"},
            {"tipo": "execucao_fiscal", "identificador": "EF-1"},
        ],
    }

    state = await workflow.execute(
        state,
        {"forma_pagamento_a_vista": "pix_copia_e_cola"},
    )

    descricao = state.agent_response.description
    assert "PIX-1" in descricao
    assert "PIX-2" in descricao
    assert "2 guias" in descricao

    guia_publica = state.agent_response.data["guia_pagamento_a_vista"]
    assert guia_publica["total_guias"] == 2
    assert [item["pix"] for item in guia_publica["guias"]] == ["PIX-1", "PIX-2"]
    # Campos no topo seguem existindo para quem lê uma guia só: a primeira.
    assert guia_publica["pix"] == "PIX-1"


@pytest.mark.asyncio
async def test_forma_pagamento_sem_guia_retorna_erro(
    divida_ativa_modules,
):
    class FakeAPIService:
        async def emitir_guia_a_vista(self, cdas, efs):
            return []

    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    workflow._api_service = FakeAPIService()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_nao_parcelado,
        "debitos_pagamento_a_vista": [
            {"tipo": "cda", "identificador": "CDA-1"},
        ],
    }

    state = await workflow.execute(
        state,
        {"forma_pagamento_a_vista": "boleto_bancario"},
    )

    assert "Não consegui emitir a guia" in state.agent_response.description
    assert state.agent_response.payload_schema is None


@pytest.mark.asyncio
async def test_acao_pagamento_recusado_invalida_retorna_botoes(
    divida_ativa_modules,
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_nao_parcelado,
    }

    state = await workflow.execute(
        state,
        {"acao_pagamento_recusado": "outra_acao"},
    )

    assert "Essa opção não existe" in state.agent_response.description
    assert (
        "acao_pagamento_recusado" in state.agent_response.payload_schema["properties"]
    )


@pytest.mark.asyncio
async def test_acao_pagamento_recusado_escolher_debitos_retorna_input(
    divida_ativa_modules,
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_nao_parcelado,
        "lista_cdas": ["CDA-1"],
        "lista_efs": ["EF-1"],
    }

    state = await workflow.execute(
        state,
        {"acao_pagamento_recusado": "escolher_debitos"},
    )

    assert "numerados de 1 a 2" in state.agent_response.description
    assert "debitos_escolhidos" in state.agent_response.payload_schema["properties"]


@pytest.mark.asyncio
async def test_acao_pagamento_recusado_opcoes_pagamento_retorna_menu(
    divida_ativa_modules,
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_nao_parcelado,
    }

    state = await workflow.execute(
        state,
        {"acao_pagamento_recusado": "opcoes_pagamento"},
    )

    assert "seguir com o pagamento" in state.agent_response.description
    assert "opcao_menu" in state.agent_response.payload_schema["properties"]


@pytest.mark.asyncio
async def test_acao_pagamento_recusado_encerrar_finaliza(
    divida_ativa_modules,
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_nao_parcelado,
    }

    state = await workflow.execute(
        state,
        {"acao_pagamento_recusado": "encerrar_atendimento"},
    )

    assert state.status == "completed"
    assert state.agent_response.description == (
        "Tudo bem! A *Prefeitura do Rio* agradece a sua confiança.\n"
        "Seu atendimento será finalizado. Obrigada!"
    )
    assert state.agent_response.payload_schema is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("opcao_menu", "texto_esperado"),
    [
        (
            "parcelar_debitos",
            "https://carioca.rio/servicos/parcelamento-em-divida-ativa/",
        ),
        (
            "liquidar_parcelamento",
            "https://daminternet.rio.rj.gov.br/GuiaPagamento/Liquidacao",
        ),
        (
            "emitir_2_via",
            "https://daminternet.rio.rj.gov.br/GuiaPagamento/EmitirSegundaVia",
        ),
    ],
)
async def test_opcoes_menu_informativas_retornam_mensagem(
    divida_ativa_modules,
    opcao_menu,
    texto_esperado,
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_completo,
    }

    state = await workflow.execute(state, {"opcao_menu": opcao_menu})

    assert texto_esperado in state.agent_response.description
    assert state.agent_response.payload_schema is None
    assert state.status == "completed"
    assert state.data == {}
    assert state.internal == {}
    assert state.agent_response.data == {"status": "completed"}


@pytest.mark.asyncio
async def test_regularizar_debitos_com_uma_guia_pula_todas_e_confirma_debitos(
    divida_ativa_modules,
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_completo,
        "lista_cdas": ["CDA-1"],
        "lista_efs": [],
        "lista_guias": ["GUIA-1"],
    }

    state = await workflow.execute(state, {"opcao_menu": "regularizar_debitos"})

    assert "digite *TODAS*" not in state.agent_response.description
    assert "Os débitos escolhidos foram" in state.agent_response.description
    assert "1. CDA-1" in state.agent_response.description
    assert (
        "confirmar_pagamento_a_vista"
        in state.agent_response.payload_schema["properties"]
    )
    assert state.data["opcao_pagar_a_vista"] == "pagar_tudo"
    assert state.data["divida_ativa"]["opcao_pagar_a_vista"] == "pagar_tudo"


@pytest.mark.asyncio
async def test_regularizar_debitos_com_multiplas_guias_retorna_botoes_de_escolha(
    divida_ativa_modules,
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_completo,
        "lista_cdas": ["CDA-1"],
        "lista_efs": [],
        "lista_guias": ["GUIA-1", "GUIA-2"],
    }

    state = await workflow.execute(state, {"opcao_menu": "regularizar_debitos"})

    schema = state.agent_response.payload_schema
    assert "pagar tudo" in state.agent_response.description
    assert "escolher os débitos" in state.agent_response.description
    assert schema["x-render"] == "buttons"
    assert schema["properties"]["opcao_pagar_a_vista"]["enum"] == [
        "pagar_tudo",
        "escolher_debitos",
    ]
    assert state.data["divida_ativa"]["opcao_menu_selecionada"] == "regularizar_debitos"


@pytest.mark.asyncio
async def test_primeira_chamada_com_cpf_cnpj_sem_debitos_mantem_mesmo_step(
    divida_ativa_modules,
):
    class FakeAPIService:
        def __init__(self):
            self.calls = []

        async def consultar_debitos_por_no(self, node_name, valor, **dados):
            self.calls.append((node_name, valor, dados))
            return None

    api_service = FakeAPIService()
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    workflow._api_service = api_service

    state = await workflow.execute(
        _new_state(divida_ativa_modules),
        {"cpf_cnpj": "12.345.678/0001-90"},
    )

    assert api_service.calls == [("consultar_cpf_cnpj", "12.345.678/0001-90", {})]
    assert state.internal["consulta_realizada"] is True
    assert state.internal["tipo_consulta_cache"] == "cpf_cnpj"
    assert "cpf_cnpj" in state.agent_response.payload_schema["properties"]
    assert "Não encontrei débitos" in state.agent_response.description


@pytest.mark.asyncio
async def test_tipo_consulta_invalido_mantem_schema(divida_ativa_modules):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = await workflow.execute(_new_state(divida_ativa_modules), {})
    state = await workflow.execute(state, {"continuar": True})

    state = await workflow.execute(state, {"tipo_consulta": "renavam"})

    assert "Essa opção não está disponível" in state.agent_response.description
    assert state.agent_response.error_message
    assert "tipo_consulta" in state.agent_response.payload_schema["properties"]
    assert "tipo_consulta" not in state.data


@pytest.mark.asyncio
async def test_input_inesperado_no_tipo_consulta_reenvia_schema(
    divida_ativa_modules,
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = await workflow.execute(_new_state(divida_ativa_modules), {})
    state = await workflow.execute(state, {"continuar": True})

    state = await workflow.execute(state, {"texto_livre": "cpf"})

    assert "Essa opção não está disponível" in state.agent_response.description
    assert "tipo_consulta" in state.agent_response.payload_schema["properties"]


@pytest.mark.asyncio
async def test_input_inesperado_no_identificador_reenvia_schema(
    divida_ativa_modules,
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = await workflow.execute(_new_state(divida_ativa_modules), {})
    state = await workflow.execute(state, {"continuar": True})
    state = await workflow.execute(state, {"tipo_consulta": "cpf_cnpj"})

    state = await workflow.execute(state, {"documento": "12345678901"})

    assert "Não consegui entender esse input" in state.agent_response.description
    assert "cpf_cnpj" in state.agent_response.payload_schema["properties"]
    assert state.internal["tipo_consulta_cache"] == "cpf_cnpj"


@pytest.mark.asyncio
async def test_input_inesperado_no_botao_pagar_a_vista_reenvia_schema(
    divida_ativa_modules,
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    state = _new_state(divida_ativa_modules)
    state.internal["consulta_realizada"] = True
    state.data["divida_ativa"] = {
        "mensagem_divida_contribuinte": "mensagem",
        "opcoes_menu": workflow.opcoes_menu_nao_parcelado,
        "lista_cdas": ["CDA-1"],
        "lista_efs": [],
    }

    state = await workflow.execute(state, {"opcao_menu": "pagar_a_vista"})
    state = await workflow.execute(state, {"texto_livre": "pagar tudo"})

    assert "Essa opção não existe" in state.agent_response.description
    assert "opcao_pagar_a_vista" in state.agent_response.payload_schema["properties"]


@pytest.mark.asyncio
async def test_fluxo_completo_pagar_tudo_boleto_bancario(
    divida_ativa_modules,
):
    resultado = _resultado_divida(
        cdas=[types.SimpleNamespace(cda_id="CDA-1", numero=None)],
        efs=[types.SimpleNamespace(numero_execucao_fiscal="EF-1", numero_ef=None)],
    )
    api_service = FakeDividaAtivaAPIService(resultado=resultado)
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    workflow._api_service = api_service

    state = await workflow.execute(_new_state(divida_ativa_modules), {})
    assert "tipo_consulta" in state.agent_response.payload_schema["properties"]

    state = await workflow.execute(state, {"tipo_consulta": "cpf_cnpj"})
    assert "cpf_cnpj" in state.agent_response.payload_schema["properties"]

    state = await workflow.execute(state, {"cpf_cnpj": "12.345.678/0001-90"})
    assert "Tipo de consulta:" in state.agent_response.description
    assert "acao_resultado" in state.agent_response.payload_schema["properties"]
    assert (
        state.data["divida_ativa"]["opcoes_menu"] == workflow.opcoes_menu_nao_parcelado
    )

    state = await workflow.execute(state, {"acao_resultado": "pagar_agora"})
    assert "opcao_menu" in state.agent_response.payload_schema["properties"]

    state = await workflow.execute(state, {"opcao_menu": "pagar_a_vista"})
    assert "opcao_pagar_a_vista" in state.agent_response.payload_schema["properties"]

    state = await workflow.execute(state, {"opcao_pagar_a_vista": "pagar_tudo"})
    assert (
        "confirmar_pagamento_a_vista"
        in state.agent_response.payload_schema["properties"]
    )
    assert state.data["divida_ativa"]["debitos_pagamento_a_vista_labels"] == [
        "1. CDA-1",
        "2. EF-1",
    ]

    state = await workflow.execute(state, {"confirmar_pagamento_a_vista": "sim"})
    assert (
        "forma_pagamento_a_vista" in state.agent_response.payload_schema["properties"]
    )

    state = await workflow.execute(
        state, {"forma_pagamento_a_vista": "boleto_bancario"}
    )
    assert "https://example.com/guia.pdf" in state.agent_response.description
    assert state.agent_response.payload_schema is None
    assert api_service.consulta_calls == [
        ("consultar_cpf_cnpj", "12.345.678/0001-90", {})
    ]
    assert api_service.emissao_calls == [(["CDA-1"], ["EF-1"])]


@pytest.mark.asyncio
async def test_guia_emitida_retorna_payload_publico_compacto(
    divida_ativa_modules,
):
    resultado = _resultado_divida(
        cdas=[types.SimpleNamespace(cda_id="CDA-1", numero=None)],
    )
    api_service = FakeDividaAtivaAPIService(
        resultado=resultado,
        guias={
            "api_resposta_sucesso": True,
            "data_vencimento": "10/09/2026",
            "link": "https://example.com/guia.pdf",
            "codigo_de_barras": "123456789",
            "pix": "000201PIX",
            "arquivoBase64": "A" * 10000,
        },
    )
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    workflow._api_service = api_service

    state = await workflow.execute(_new_state(divida_ativa_modules), {})
    state = await workflow.execute(state, {"tipo_consulta": "cpf_cnpj"})
    state = await workflow.execute(state, {"cpf_cnpj": "12345678901"})
    state = await workflow.execute(state, {"acao_resultado": "pagar_agora"})
    state = await workflow.execute(state, {"opcao_menu": "pagar_a_vista"})
    state = await workflow.execute(state, {"opcao_pagar_a_vista": "pagar_tudo"})
    state = await workflow.execute(state, {"confirmar_pagamento_a_vista": "sim"})
    state = await workflow.execute(
        state,
        {"forma_pagamento_a_vista": "boleto_bancario"},
    )

    data = state.agent_response.data
    data_json = json.dumps(data, ensure_ascii=False)

    assert state.status == "completed"
    assert data["status"] == "completed"
    assert state.data == {}
    assert state.internal == {}
    guia_publica = {
        "data_vencimento": "10/09/2026",
        "link": "https://example.com/guia.pdf",
        "codigo_de_barras": "123456789",
        "pix": "000201PIX",
    }
    assert data["guia_pagamento_a_vista"] == {
        **guia_publica,
        "guias": [guia_publica],
        "total_guias": 1,
    }
    assert "arquivoBase64" not in data_json
    assert "A" * 1000 not in data_json
    assert "proximo_payload" not in data


@pytest.mark.asyncio
async def test_fluxo_completo_escolher_debitos_e_pix(
    divida_ativa_modules,
):
    resultado = _resultado_divida(
        cdas=[
            types.SimpleNamespace(cda_id="CDA-1", numero=None),
            types.SimpleNamespace(cda_id="CDA-2", numero=None),
        ],
        efs=[types.SimpleNamespace(numero_execucao_fiscal="EF-1", numero_ef=None)],
    )
    api_service = FakeDividaAtivaAPIService(resultado=resultado)
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    workflow._api_service = api_service

    state = await workflow.execute(_new_state(divida_ativa_modules), {})
    state = await workflow.execute(state, {"continuar": True})
    state = await workflow.execute(state, {"tipo_consulta": "cpf_cnpj"})
    state = await workflow.execute(state, {"cpf_cnpj": "12.345.678/0001-90"})
    state = await workflow.execute(state, {"acao_resultado": "pagar_agora"})
    state = await workflow.execute(state, {"opcao_menu": "pagar_a_vista"})

    state = await workflow.execute(
        state,
        {"opcao_pagar_a_vista": "escolher_debitos"},
    )
    assert "debitos_escolhidos" in state.agent_response.payload_schema["properties"]

    state = await workflow.execute(state, {"debitos_escolhidos": "2, 3"})
    assert "2. CDA-2" in state.agent_response.description
    assert "3. EF-1" in state.agent_response.description
    assert "1. CDA-1" not in state.agent_response.description

    state = await workflow.execute(state, {"confirmar_pagamento_a_vista": "sim"})
    state = await workflow.execute(
        state, {"forma_pagamento_a_vista": "pix_copia_e_cola"}
    )

    assert "000201PIX" in state.agent_response.description
    assert api_service.emissao_calls == [(["CDA-2"], ["EF-1"])]


@pytest.mark.asyncio
async def test_fluxo_nao_opcoes_pagamento_e_parcelar(
    divida_ativa_modules,
):
    resultado = _resultado_divida(
        cdas=[types.SimpleNamespace(cda_id="CDA-1", numero=None)],
    )
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    workflow._api_service = FakeDividaAtivaAPIService(resultado=resultado)

    state = await workflow.execute(_new_state(divida_ativa_modules), {})
    state = await workflow.execute(state, {"continuar": True})
    state = await workflow.execute(state, {"tipo_consulta": "cpf_cnpj"})
    state = await workflow.execute(state, {"cpf_cnpj": "12345678901"})
    state = await workflow.execute(state, {"acao_resultado": "pagar_agora"})
    state = await workflow.execute(state, {"opcao_menu": "pagar_a_vista"})
    state = await workflow.execute(state, {"opcao_pagar_a_vista": "pagar_tudo"})

    state = await workflow.execute(state, {"confirmar_pagamento_a_vista": "nao"})
    assert (
        "acao_pagamento_recusado" in state.agent_response.payload_schema["properties"]
    )

    state = await workflow.execute(
        state, {"acao_pagamento_recusado": "opcoes_pagamento"}
    )
    assert "opcao_menu" in state.agent_response.payload_schema["properties"]

    state = await workflow.execute(state, {"opcao_menu": "parcelar_debitos"})
    assert "parcelamento-em-divida-ativa" in state.agent_response.description
    assert state.agent_response.payload_schema is None


@pytest.mark.asyncio
async def test_fluxo_consultar_outro_debito_reinicia_identificador(
    divida_ativa_modules,
):
    resultado = _resultado_divida(
        cdas=[types.SimpleNamespace(cda_id="CDA-1", numero=None)],
    )
    workflow = divida_ativa_modules.DividaAtivaWorkflow()
    workflow._api_service = FakeDividaAtivaAPIService(resultado=resultado)

    state = await workflow.execute(_new_state(divida_ativa_modules), {})
    state = await workflow.execute(state, {"continuar": True})
    state = await workflow.execute(state, {"tipo_consulta": "cpf_cnpj"})
    state = await workflow.execute(state, {"cpf_cnpj": "12345678901"})

    assert "divida_ativa" in state.data
    state = await workflow.execute(state, {"acao_resultado": "consultar_outro_debito"})

    assert "divida_ativa" not in state.data
    assert "consulta_realizada" not in state.internal
    assert "tipo_consulta" in state.agent_response.payload_schema["properties"]


@pytest.mark.asyncio
async def test_fluxo_input_inesperado_nao_avanca_step(
    divida_ativa_modules,
):
    workflow = divida_ativa_modules.DividaAtivaWorkflow()

    state = await workflow.execute(_new_state(divida_ativa_modules), {})
    state = await workflow.execute(state, {"continuar": True})
    state = await workflow.execute(state, {"tipo_consulta": "cpf_cnpj"})

    state = await workflow.execute(state, {"documento": "12345678901"})
    assert "cpf_cnpj" in state.agent_response.payload_schema["properties"]
    assert state.internal["tipo_consulta_cache"] == "cpf_cnpj"
    assert "consulta_realizada" not in state.internal
