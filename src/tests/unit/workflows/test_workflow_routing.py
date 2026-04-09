import importlib.util
import sys
import types
from pathlib import Path

import pytest


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
def service_models():
    return sys.modules["src.tools.multi_step_service.core.models"]


@pytest.fixture
def poda_workflow_module(monkeypatch, service_models):
    _ensure_package("src", PROJECT_ROOT / "src")
    _ensure_package("src.config", PROJECT_ROOT / "src" / "config")
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
        "src.tools.multi_step_service.workflows.poda_de_arvore",
        PROJECT_ROOT
        / "src"
        / "tools"
        / "multi_step_service"
        / "workflows"
        / "poda_de_arvore",
    )
    _ensure_package(
        "src.tools.multi_step_service.workflows.poda_de_arvore.integrations",
        PROJECT_ROOT
        / "src"
        / "tools"
        / "multi_step_service"
        / "workflows"
        / "poda_de_arvore"
        / "integrations",
    )
    _ensure_package(
        "src.tools.multi_step_service.workflows.poda_de_arvore.api",
        PROJECT_ROOT
        / "src"
        / "tools"
        / "multi_step_service"
        / "workflows"
        / "poda_de_arvore"
        / "api",
    )
    _ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")

    monkeypatch.setitem(
        sys.modules, "src.config.env", types.SimpleNamespace(PODA_SERVICE_ID="svc-123")
    )
    monkeypatch.setitem(
        sys.modules,
        "src.tools.multi_step_service.core.base_workflow",
        types.SimpleNamespace(
            BaseWorkflow=type("BaseWorkflow", (), {"__init__": lambda self: None}),
            handle_errors=lambda func: func,
        ),
    )

    templates_module = _load_module(
        "src.tools.multi_step_service.workflows.poda_de_arvore.templates",
        "src/tools/multi_step_service/workflows/poda_de_arvore/templates.py",
    )

    monkeypatch.setitem(
        sys.modules,
        "src.tools.multi_step_service.workflows.poda_de_arvore.integrations",
        types.SimpleNamespace(
            build_ticket_payload=lambda *args, **kwargs: {"ok": True}
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.tools.multi_step_service.workflows.poda_de_arvore.api.api_service",
        types.SimpleNamespace(
            SGRCAPIService=type("SGRCAPIService", (), {}),
            AddressAPIService=type("AddressAPIService", (), {}),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.typesense_api",
        types.SimpleNamespace(
            HubSearchRequest=lambda **kwargs: types.SimpleNamespace(**kwargs),
            hub_search_by_id=lambda request: None,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "prefeitura_rio.integrations.sgrc",
        types.SimpleNamespace(async_new_ticket=lambda **kwargs: None),
    )
    monkeypatch.setitem(
        sys.modules,
        "prefeitura_rio.integrations.sgrc.models",
        types.SimpleNamespace(Address=object, Requester=object),
    )
    exc_module = types.SimpleNamespace(
        SGRCBusinessRuleException=Exception,
        SGRCInvalidBodyException=Exception,
        SGRCMalformedBodyException=Exception,
        SGRCDuplicateTicketException=Exception,
        SGRCEquivalentTicketException=Exception,
        SGRCInternalErrorException=Exception,
    )
    monkeypatch.setitem(
        sys.modules, "prefeitura_rio.integrations.sgrc.exceptions", exc_module
    )

    monkeypatch.setitem(
        sys.modules,
        "src.tools.multi_step_service.workflows.poda_de_arvore",
        types.SimpleNamespace(templates=templates_module),
    )

    return _load_module(
        "test_poda_workflow_module",
        "src/tools/multi_step_service/workflows/poda_de_arvore/workflow.py",
    )


@pytest.fixture
def iptu_workflow_module(monkeypatch, service_models):
    _ensure_package("src", PROJECT_ROOT / "src")
    _ensure_package("src.config", PROJECT_ROOT / "src" / "config")
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
        "src.tools.multi_step_service.workflows.iptu_pagamento",
        PROJECT_ROOT
        / "src"
        / "tools"
        / "multi_step_service"
        / "workflows"
        / "iptu_pagamento",
    )
    _ensure_package(
        "src.tools.multi_step_service.workflows.iptu_pagamento.api",
        PROJECT_ROOT
        / "src"
        / "tools"
        / "multi_step_service"
        / "workflows"
        / "iptu_pagamento"
        / "api",
    )
    _ensure_package(
        "src.tools.multi_step_service.workflows.iptu_pagamento.helpers",
        PROJECT_ROOT
        / "src"
        / "tools"
        / "multi_step_service"
        / "workflows"
        / "iptu_pagamento"
        / "helpers",
    )
    _ensure_package(
        "src.tools.multi_step_service.workflows.iptu_pagamento.core",
        PROJECT_ROOT
        / "src"
        / "tools"
        / "multi_step_service"
        / "workflows"
        / "iptu_pagamento"
        / "core",
    )

    monkeypatch.setitem(sys.modules, "src.config.env", types.SimpleNamespace())

    core_module = types.SimpleNamespace(
        AgentResponse=service_models.AgentResponse,
        BaseWorkflow=type(
            "BaseWorkflow", (), {"__init__": lambda self: None, "_user_id": "unknown"}
        ),
        ServiceState=service_models.ServiceState,
        handle_errors=lambda func: func,
    )
    monkeypatch.setitem(sys.modules, "src.tools.multi_step_service.core", core_module)

    monkeypatch.setitem(
        sys.modules,
        "src.tools.multi_step_service.workflows.iptu_pagamento.api.api_service",
        types.SimpleNamespace(
            IPTUAPIService=lambda user_id: types.SimpleNamespace(user_id=user_id)
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.tools.multi_step_service.workflows.iptu_pagamento.api.api_service_fake",
        types.SimpleNamespace(
            IPTUAPIServiceFake=lambda: types.SimpleNamespace(fake=True)
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.tools.multi_step_service.workflows.iptu_pagamento.api.exceptions",
        types.SimpleNamespace(
            APIUnavailableError=type("APIUnavailableError", (Exception,), {}),
            AuthenticationError=type("AuthenticationError", (Exception,), {}),
            InvalidInscricaoError=type("InvalidInscricaoError", (Exception,), {}),
        ),
    )

    templates_module = _load_module(
        "test_iptu_templates_module",
        "src/tools/multi_step_service/workflows/iptu_pagamento/templates.py",
    )
    monkeypatch.setitem(
        sys.modules,
        "src.tools.multi_step_service.workflows.iptu_pagamento.templates",
        templates_module,
    )

    return _load_module(
        "test_iptu_workflow_module",
        "src/tools/multi_step_service/workflows/iptu_pagamento/iptu_workflow.py",
    )


def test_poda_helpers_and_reset_paths(poda_workflow_module, service_models):
    workflow = poda_workflow_module.PodaDeArvoreWorkflow(use_fake_api=True)
    state = service_models.ServiceState(
        user_id="u1",
        service_name="poda_de_arvore",
        payload={"address": "Rua A, 10"},
    )

    address = {
        "logradouro_nome_ipp": "Rua A",
        "numero": "10",
        "bairro_nome_ipp": "Centro",
    }
    assert "Rua A" in workflow.format_address_confirmation(address)

    state.data.update(
        {
            "address_validated": True,
            "address_confirmed": True,
            "ticket_created": False,
        }
    )
    assert bool(workflow._has_valid_confirmed_address(state)) is True

    state.data["awaiting_user_memory_confirmation"] = True
    assert bool(workflow._has_valid_confirmed_address(state)) is False

    workflow._clear_address_data(state)
    assert "address_validated" not in state.data
    assert "address_confirmed" not in state.data

    assert workflow.increment_attempts(state, "cpf_attempts") == 1
    assert workflow.increment_attempts(state, "cpf_attempts") == 2

    state.data.update(
        {
            "ticket_created": True,
            "error": "oops",
            "cpf": "123",
            "email": "x@y.com",
            "name": "Nome",
        }
    )
    state.payload = {}
    workflow._reset_previous_session_flags(state)
    assert "ticket_created" not in state.data
    assert state.data["personal_data_needs_confirmation"] is True

    state.data["restarting_after_error"] = True
    state.data["error_message"] = "falhou"
    handled = workflow._handle_restart_after_error(state)
    assert handled is True
    assert "falhou" in state.agent_response.description


@pytest.mark.asyncio
async def test_poda_initialize_and_routing(
    poda_workflow_module, service_models, monkeypatch
):
    workflow = poda_workflow_module.PodaDeArvoreWorkflow(use_fake_api=True)
    state = service_models.ServiceState(user_id="u1", service_name="poda_de_arvore")

    async def fake_load():
        workflow.service_knowledge = {
            "title": "Poda",
            "resumo": "Resumo",
            "tempo_atendimento": "5 dias",
            "custo_servico": "Sem custo",
        }

    monkeypatch.setattr(workflow, "_load_service_knowledge", fake_load)
    result = await workflow._initialize_workflow(state)
    assert result.data["knowledge_loaded"] is True
    assert result.data["service_info"]["nome"] == "Poda"
    assert result.agent_response is None

    route_state = service_models.ServiceState(
        user_id="u1",
        service_name="poda_de_arvore",
        data={"address_needs_confirmation": True},
    )
    assert workflow._route_after_address(route_state) == "confirm_address"

    route_state = service_models.ServiceState(
        user_id="u1",
        service_name="poda_de_arvore",
        data={
            "address_validated": True,
            "address_confirmed": True,
            "reference_point_collected": True,
            "cpf": "123",
            "email_processed": True,
            "name_processed": True,
        },
    )
    assert workflow._route_after_address(route_state) == "confirm_ticket_data"

    cpf_state = service_models.ServiceState(
        user_id="u1",
        service_name="poda_de_arvore",
        data={"cpf_max_attempts_reached": True},
    )
    assert workflow._route_after_cpf(cpf_state) == "collect_email"

    cpf_state = service_models.ServiceState(
        user_id="u1",
        service_name="poda_de_arvore",
        data={"email_processed": True, "name_processed": True},
    )
    assert workflow._route_after_cpf(cpf_state) == "confirm_ticket_data"

    confirm_state = service_models.ServiceState(
        user_id="u1",
        service_name="poda_de_arvore",
        data={"ticket_data_confirmed": True},
    )
    assert workflow._route_after_ticket_confirmation(confirm_state) == "open_ticket"

    confirm_state = service_models.ServiceState(
        user_id="u1",
        service_name="poda_de_arvore",
        data={"correction_requested": "email"},
    )
    assert workflow._route_after_ticket_confirmation(confirm_state) == "collect_email"

    email_state = service_models.ServiceState(
        user_id="u1",
        service_name="poda_de_arvore",
        data={"email_processed": True},
    )
    assert workflow._route_after_email(email_state) == "collect_name"

    name_state = service_models.ServiceState(
        user_id="u1",
        service_name="poda_de_arvore",
        data={"name_processed": True},
    )
    assert workflow._route_after_name(name_state) == "confirm_ticket_data"


@pytest.mark.asyncio
async def test_poda_confirm_address_and_reference_point(
    poda_workflow_module, service_models
):
    workflow = poda_workflow_module.PodaDeArvoreWorkflow(use_fake_api=True)

    state = service_models.ServiceState(
        user_id="u1",
        service_name="poda_de_arvore",
        data={
            "address_needs_confirmation": True,
            "address_temp": {"logradouro_nome_ipp": "Rua A", "numero": "10"},
        },
        payload={"confirmacao": True},
    )
    result = await workflow._confirm_address(state)
    assert result.data["address_confirmed"] is True
    assert result.data["need_reference_point"] is True
    assert result.agent_response is None

    state = service_models.ServiceState(
        user_id="u1",
        service_name="poda_de_arvore",
        data={
            "address_needs_confirmation": True,
            "address_temp": {"logradouro_nome_ipp": "Rua B", "numero": "20"},
            "address_validation": {"attempts": 1, "max_attempts": 3},
        },
        payload={"confirmacao": False},
    )
    result = await workflow._confirm_address(state)
    assert "informe novamente o endereço" in result.agent_response.description.lower()
    assert result.data["address_validated"] is False

    state = service_models.ServiceState(
        user_id="u1",
        service_name="poda_de_arvore",
        data={"need_reference_point": True},
    )
    result = await workflow._collect_reference_point(state)
    assert "ponto de referência" in result.agent_response.description.lower()

    state.payload = {"ponto_referencia": "Em frente à praça"}
    result = await workflow._collect_reference_point(state)
    assert result.data["reference_point_collected"] is True
    assert result.data["ponto_referencia"] == "Em frente à praça"
    assert result.agent_response is None


@pytest.mark.asyncio
async def test_poda_collect_cpf_email_and_name_branches(
    poda_workflow_module, service_models
):
    workflow = poda_workflow_module.PodaDeArvoreWorkflow(use_fake_api=True)

    state = service_models.ServiceState(
        user_id="u1",
        service_name="poda_de_arvore",
        data={"awaiting_user_memory_confirmation": True, "name": "Maria Silva"},
        payload={"confirmacao": False},
    )
    result = await workflow._collect_cpf(state)
    assert "informe seu cpf" in result.agent_response.description.lower()
    assert "awaiting_user_memory_confirmation" not in result.data
    assert "cpf" not in result.data

    state = service_models.ServiceState(
        user_id="u1",
        service_name="poda_de_arvore",
        payload={"cpf": "123.456.789-09"},
    )
    result = await workflow._collect_cpf(state)
    assert result.data["cpf"] == "12345678909"
    assert result.agent_response is None

    state = service_models.ServiceState(
        user_id="u1",
        service_name="poda_de_arvore",
        data={"cpf_attempts": 2},
        payload={"cpf": "11111111111"},
    )
    result = await workflow._collect_cpf(state)
    assert result.data["identificacao_pulada"] is True
    assert result.data["cpf_max_attempts_reached"] is True

    state = service_models.ServiceState(
        user_id="u1",
        service_name="poda_de_arvore",
        payload={"email": ""},
    )
    result = await workflow._collect_email(state)
    assert result.data["email_skipped"] is True
    assert result.data["email_processed"] is True

    state = service_models.ServiceState(
        user_id="u1",
        service_name="poda_de_arvore",
        payload={"email": "USER@Example.COM"},
    )
    result = await workflow._collect_email(state)
    assert result.data["email"] == "user@example.com"
    assert result.data["email_processed"] is True

    state = service_models.ServiceState(
        user_id="u1",
        service_name="poda_de_arvore",
        data={"name_attempts": 2},
        payload={"name": "A"},
    )
    result = await workflow._collect_name(state)
    assert result.data["name_skipped"] is True
    assert result.data["name_processed"] is True

    state = service_models.ServiceState(
        user_id="u1",
        service_name="poda_de_arvore",
        payload={"name": "maria da silva"},
    )
    result = await workflow._collect_name(state)
    assert result.data["name"] == "Maria Da Silva"
    assert result.data["name_processed"] is True


@pytest.mark.asyncio
async def test_poda_confirm_ticket_data_and_open_ticket(
    poda_workflow_module, service_models, monkeypatch
):
    workflow = poda_workflow_module.PodaDeArvoreWorkflow(use_fake_api=True)

    state = service_models.ServiceState(
        user_id="u1",
        service_name="poda_de_arvore",
        data={
            "address": {"logradouro_nome_ipp": "Rua A", "numero": "10"},
            "ponto_referencia": "Praça",
            "name": "Maria Silva",
            "cpf": "12345678909",
            "email": "maria@example.com",
            "phone": "21999999999",
        },
    )
    result = await workflow._confirm_ticket_data(state)
    assert "poda de árvore" in result.agent_response.description.lower()

    state.payload = {"confirmacao": True}
    result = await workflow._confirm_ticket_data(state)
    assert result.data["ticket_data_confirmed"] is True
    assert result.agent_response is None

    state = service_models.ServiceState(
        user_id="u1",
        service_name="poda_de_arvore",
        data={
            "address": {"logradouro_nome_ipp": "Rua A", "numero": "10"},
            "address_confirmed": True,
            "address_validated": True,
        },
        payload={"confirmacao": False, "correcao": "quero corrigir o endereço"},
    )
    result = await workflow._confirm_ticket_data(state)
    assert result.data["correction_requested"] == "address"
    assert "informe o endereço correto" in result.agent_response.description.lower()

    state = service_models.ServiceState(user_id="u1", service_name="poda_de_arvore")
    result = await workflow._open_ticket(state)
    assert result.data["ticket_created"] is True
    assert result.data["protocol_id"].startswith("FAKE-")

    failing_workflow = poda_workflow_module.PodaDeArvoreWorkflow(use_fake_api=False)
    monkeypatch.setattr(
        poda_workflow_module,
        "build_ticket_payload",
        lambda state: ("address", "requester", "description"),
    )

    async def raise_value_error(**_kwargs):
        raise ValueError("payload inválido")

    monkeypatch.setattr(failing_workflow, "new_ticket", raise_value_error)
    state = service_models.ServiceState(user_id="u1", service_name="poda_de_arvore")
    result = await failing_workflow._open_ticket(state)
    assert result.data["error"] == "erro_interno"
    assert result.data["ticket_created"] is False


def test_iptu_constructor_api_service_and_routes(
    iptu_workflow_module, service_models, monkeypatch
):
    monkeypatch.setattr(
        iptu_workflow_module.os, "getenv", lambda *_args, **_kwargs: "false"
    )
    workflow = iptu_workflow_module.IPTUWorkflow(use_fake_api=False)
    workflow._user_id = "user-1"
    service = workflow.api_service
    assert service.user_id == "user-1"

    monkeypatch.setattr(
        iptu_workflow_module.os, "getenv", lambda *_args, **_kwargs: "true"
    )
    fake_workflow = iptu_workflow_module.IPTUWorkflow(use_fake_api=False)
    assert fake_workflow.api_service.fake is True

    state = service_models.ServiceState(user_id="u1", service_name="iptu_pagamento")
    assert workflow._decide_after_data_collection(state) == "continue"
    state.agent_response = service_models.AgentResponse(description="erro")
    assert workflow._decide_after_data_collection(state) == iptu_workflow_module.END

    route_state = service_models.ServiceState(
        user_id="u1",
        service_name="iptu_pagamento",
        data={"dados_guias": {"ok": True}},
    )
    assert workflow._route_consulta_guias(route_state) == "usuario_escolhe_guias"

    route_state = service_models.ServiceState(
        user_id="u1",
        service_name="iptu_pagamento",
        data={"inscricao_imobiliaria": "123"},
    )
    assert workflow._route_consulta_guias(route_state) == "escolher_ano"

    route_state = service_models.ServiceState(
        user_id="u1", service_name="iptu_pagamento"
    )
    assert workflow._route_consulta_guias(route_state) == "informar_inscricao"

    cotas_state = service_models.ServiceState(
        user_id="u1",
        service_name="iptu_pagamento",
        data={"dados_cotas": {"ok": True}},
    )
    assert workflow._route_consulta_cotas(cotas_state) == "usuario_escolhe_cotas"
    cotas_state = service_models.ServiceState(
        user_id="u1", service_name="iptu_pagamento"
    )
    assert workflow._route_consulta_cotas(cotas_state) == "usuario_escolhe_guias"


@pytest.mark.asyncio
async def test_iptu_confirmation_and_boleto_description(
    iptu_workflow_module, service_models, monkeypatch
):
    workflow = iptu_workflow_module.IPTUWorkflow(use_fake_api=True)
    monkeypatch.setattr(
        iptu_workflow_module.state_helpers,
        "validar_dados_obrigatorios",
        lambda state, campos: "ano_exercicio",
    )
    monkeypatch.setattr(
        iptu_workflow_module.state_helpers,
        "reset_completo",
        lambda state, manter_inscricao=False: state.data.update({"reset": True}),
    )

    state = service_models.ServiceState(user_id="u1", service_name="iptu_pagamento")
    result = await workflow._confirmacao_dados_pagamento(state)
    assert "Campo obrigatório faltante" in result.agent_response.description
    assert result.data["reset"] is True

    monkeypatch.setattr(
        iptu_workflow_module.state_helpers,
        "validar_dados_obrigatorios",
        lambda state, campos: None,
    )
    monkeypatch.setattr(
        iptu_workflow_module.utils,
        "calcular_numero_boletos",
        lambda darm_separado, quantidade: quantidade if darm_separado else 1,
    )

    state = service_models.ServiceState(
        user_id="u1",
        service_name="iptu_pagamento",
        data={
            "inscricao_imobiliaria": "123",
            "guia_escolhida": "00",
            "cotas_escolhidas": ["01", "02"],
            "endereco": "Rua X",
            "proprietario": "João",
        },
        internal={iptu_workflow_module.STATE_USE_SEPARATE_DARM: True},
    )
    result = await workflow._confirmacao_dados_pagamento(state)
    assert result.agent_response.payload_schema is not None

    state.payload = {"confirmacao": False}
    result = await workflow._confirmacao_dados_pagamento(state)
    assert "não confirmados" in result.agent_response.description.lower()

    state.payload = {"confirmacao": True}
    result = await workflow._confirmacao_dados_pagamento(state)
    assert result.internal[iptu_workflow_module.STATE_IS_DATA_CONFIRMED] is True
    assert result.agent_response is None

    monkeypatch.setattr(
        iptu_workflow_module.utils,
        "preparar_dados_boletos_para_template",
        lambda guias: [
            {
                "numero_guia": "00",
                "cotas": "01, 02",
                "valor": 123.45,
                "vencimento": "10/04/2026",
                "codigo_barras": "123",
                "linha_digitavel": "456",
                "pdf": "https://example.com/darm.pdf",
            }
        ],
    )
    state = service_models.ServiceState(
        user_id="u1",
        service_name="iptu_pagamento",
        data={"guias_geradas": [{"numero": "1"}], "inscricao_imobiliaria": "123"},
    )
    description = workflow._gerar_descricao_boletos_gerados(state)
    assert "123" in description
