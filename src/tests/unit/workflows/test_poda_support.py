import importlib.util
import sys
import types
from pathlib import Path

import pytest

from src.tools.multi_step_service.core.models import AgentResponse, ServiceState


poda_models = sys.modules[
    "src.tools.multi_step_service.workflows.poda_de_arvore.models"
]
poda_state_helpers = sys.modules[
    "src.tools.multi_step_service.workflows.poda_de_arvore.state_helpers"
]
ticket_builder = sys.modules[
    "src.tools.multi_step_service.workflows.poda_de_arvore.integrations.ticket_builder"
]

PROJECT_ROOT = Path(__file__).resolve().parents[4]


def _ensure_package(name: str, path: Path):
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(path)]
    sys.modules[name] = pkg
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


def prepare_poda_api_module(monkeypatch, module_name="test_poda_api_service_module"):
    _ensure_package("src", PROJECT_ROOT / "src")
    _ensure_package("src.config", PROJECT_ROOT / "src" / "config")
    _ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")
    _ensure_package("src.tools", PROJECT_ROOT / "src" / "tools")
    _ensure_package(
        "src.tools.multi_step_service",
        PROJECT_ROOT / "src" / "tools" / "multi_step_service",
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
        "src.tools.multi_step_service.workflows.poda_de_arvore.api",
        PROJECT_ROOT
        / "src"
        / "tools"
        / "multi_step_service"
        / "workflows"
        / "poda_de_arvore"
        / "api",
    )

    env_module = types.SimpleNamespace(
        CHATBOT_INTEGRATIONS_URL="https://integrations.example/",
        CHATBOT_INTEGRATIONS_KEY="integration-key",
        GMAPS_API_TOKEN="maps-token",
        DATA_DIR=Path("/tmp"),
    )
    monkeypatch.setitem(sys.modules, "src.config.env", env_module)
    monkeypatch.setitem(
        sys.modules, "src.config", types.SimpleNamespace(env=env_module)
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.error_interceptor",
        types.SimpleNamespace(interceptor=lambda *a, **k: lambda f: f),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.http_client",
        types.SimpleNamespace(InterceptedHTTPClient=None),
    )
    monkeypatch.setitem(
        sys.modules,
        "aiohttp",
        types.SimpleNamespace(ClientSession=object),
    )
    monkeypatch.setitem(
        sys.modules,
        "async_googlemaps",
        types.SimpleNamespace(AsyncClient=object),
    )
    monkeypatch.setitem(
        sys.modules,
        "geopandas",
        types.SimpleNamespace(read_file=lambda *_args, **_kwargs: None),
    )
    monkeypatch.setitem(
        sys.modules,
        "shapely.geometry",
        types.SimpleNamespace(Point=lambda x, y: (x, y)),
    )
    monkeypatch.setitem(
        sys.modules,
        "shapely.wkt",
        types.SimpleNamespace(loads=lambda value: value),
    )

    return _load_module(
        module_name,
        "src/tools/multi_step_service/workflows/poda_de_arvore/api/api_service.py",
    )


def test_nome_payload_normaliza_caps_and_spaces():
    payload = poda_models.NomePayload.model_validate({"name": "  joão   da   silva  "})
    assert payload.name == "João Da Silva"


def test_nome_payload_rejeita_nome_sem_sobrenome():
    with pytest.raises(ValueError, match="nome e sobrenome"):
        poda_models.NomePayload.model_validate({"name": "João"})


def test_email_payload_normaliza_lowercase():
    payload = poda_models.EmailPayload.model_validate(
        {"email": "  TESTE@EXEMPLO.COM  "}
    )
    assert payload.email == "teste@exemplo.com"


def test_email_payload_rejeita_email_invalido():
    with pytest.raises(ValueError, match="Email inválido"):
        poda_models.EmailPayload.model_validate({"email": "email-invalido"})


def test_cpf_payload_strips_formatting():
    payload = poda_models.CPFPayload.model_validate({"cpf": "123.456.789-09"})
    assert payload.cpf == "12345678909"


def test_cpf_payload_accepts_empty_value():
    payload = poda_models.CPFPayload.model_validate({"cpf": ""})
    assert payload.cpf is None


def test_address_data_normalizes_cep():
    payload = poda_models.AddressData.model_validate(
        {
            "logradouro": "Rua X",
            "numero": "10",
            "bairro": "Centro",
            "cep": "22.220-333",
        }
    )
    assert payload.cep == "22220333"


def test_address_data_invalid_cep_becomes_none():
    payload = poda_models.AddressData.model_validate(
        {
            "logradouro": "Rua X",
            "numero": "10",
            "bairro": "Centro",
            "cep": "123",
        }
    )
    assert payload.cep is None


def test_ticket_opened_sets_ticket_state():
    state = ServiceState(user_id="u1", service_name="poda_de_arvore")

    result = poda_state_helpers.ticket_opened(
        state,
        protocol_id="12345",
        description="Chamado aberto com sucesso",
    )

    assert result.data["protocol_id"] == "12345"
    assert result.data["ticket_created"] is True
    assert result.agent_response == AgentResponse(
        description="Chamado aberto com sucesso"
    )


def test_ticket_failed_sets_error_state():
    state = ServiceState(user_id="u1", service_name="poda_de_arvore")

    result = poda_state_helpers.ticket_failed(
        state,
        error_code="API_ERROR",
        description="Falha ao abrir chamado",
        error_message="sem conexão",
    )

    assert result.data["ticket_created"] is False
    assert result.data["error"] == "API_ERROR"
    assert result.agent_response.description == "Falha ao abrir chamado"
    assert result.agent_response.error_message == "sem conexão"


def test_build_requester_includes_user_fields_and_phone():
    state = ServiceState(
        user_id="u1",
        service_name="poda_de_arvore",
        data={
            "email": "user@example.com",
            "cpf": "12345678909",
            "name": "Nome Sobrenome",
            "phone": "21999999999",
        },
    )

    requester = ticket_builder.build_requester(state)

    assert requester.email == "user@example.com"
    assert requester.cpf == "12345678909"
    assert requester.name == "Nome Sobrenome"
    assert requester.phones.telefone1 == "21999999999"


def test_build_address_sanitizes_number_and_prefers_ipp_fields():
    state = ServiceState(
        user_id="u1",
        service_name="poda_de_arvore",
        data={
            "address": {
                "logradouro": "Rua Original",
                "logradouro_nome_ipp": "Rua IPP",
                "logradouro_id_ipp": "123",
                "bairro": "Centro",
                "bairro_nome_ipp": "Bairro IPP",
                "bairro_id_ipp": "456",
                "numero": "10A",
                "cep": "20000-000",
            },
            "ponto_referencia": "Perto da praça",
        },
    )

    address = ticket_builder.build_address(state)

    assert address.street == "Rua IPP"
    assert address.street_code == "123"
    assert address.neighborhood == "Bairro IPP"
    assert address.neighborhood_code == "456"
    assert address.number == "10"
    assert address.locality == "Perto da praça"
    assert address.zip_code == "20000-000"


def test_build_address_defaults_number_to_one_when_missing_digits():
    state = ServiceState(
        user_id="u1",
        service_name="poda_de_arvore",
        data={
            "address": {
                "logradouro": "Rua Sem Numero",
                "bairro": "Centro",
                "numero": "S/N",
            }
        },
    )

    address = ticket_builder.build_address(state)

    assert address.number == "1"
    assert address.street == "Rua Sem Numero"
    assert address.neighborhood == "Centro"


def test_build_ticket_payload_returns_expected_tuple():
    state = ServiceState(
        user_id="u1",
        service_name="poda_de_arvore",
        data={
            "address": {"logradouro": "Rua Teste", "bairro": "Centro", "numero": "5"},
            "email": "user@example.com",
        },
    )

    address, requester, description = ticket_builder.build_ticket_payload(state)

    assert address.street == "Rua Teste"
    assert requester.email == "user@example.com"
    assert description == "poda de árvore"


@pytest.mark.asyncio
async def test_sgrc_service_get_integrations_url_and_user_info(monkeypatch):
    module = prepare_poda_api_module(monkeypatch)
    service = module.SGRCAPIService()

    assert (
        service.get_integrations_url("/person") == "https://integrations.example/person"
    )

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"name": "Maria"}

    class DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, *args, **kwargs):
            return DummyResponse()

    monkeypatch.setattr(module, "InterceptedHTTPClient", lambda **kwargs: DummyClient())
    result = await service.get_user_info("12345678909")
    assert result == {"name": "Maria"}


@pytest.mark.asyncio
async def test_sgrc_service_get_user_info_wraps_errors(monkeypatch):
    module = prepare_poda_api_module(monkeypatch, "test_poda_api_service_module_error")
    service = module.SGRCAPIService()

    class FailingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, *args, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(
        module, "InterceptedHTTPClient", lambda **kwargs: FailingClient()
    )

    with pytest.raises(Exception, match="Failed to get user info: boom"):
        await service.get_user_info("12345678909")


@pytest.mark.asyncio
async def test_address_service_helpers_and_endereco_info(monkeypatch):
    module = prepare_poda_api_module(monkeypatch, "test_poda_api_service_module_addr")
    monkeypatch.setattr(module.AddressAPIService, "_load_shape_rj", lambda self: None)
    service = module.AddressAPIService()

    assert await service.substitute_digits("Rua 12") != "Rua 12"
    assert round(service.haversine_distance(0, 0, 0, 0), 2) == 0.00

    monkeypatch.setattr(
        service,
        "get_nearest_logradouro_and_bairro",
        lambda lat, lon: module.NearestLocation(
            id_logradouro=10,
            name_logradouro="Rua IPP",
            id_bairro=20,
            name_bairro="Centro",
        ),
    )

    async def fake_get_ipp_street_code(**kwargs):
        return {"logradouro_id": "99", "bairro_nome": "Centro"}

    monkeypatch.setattr(service, "get_ipp_street_code", fake_get_ipp_street_code)
    result = await service.get_endereco_info(-22.9, -43.2, "Rua A", "Centro")
    assert result["logradouro_id"] == "99"

    monkeypatch.setattr(
        service,
        "get_nearest_logradouro_and_bairro",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("sem geo")),
    )
    result = await service.get_endereco_info(-22.9, -43.2)
    assert result["logradouro_id"] == "0"
    assert result["bairro_id"] == "0"


@pytest.mark.asyncio
async def test_address_service_get_ipp_street_code(monkeypatch):
    module = prepare_poda_api_module(monkeypatch, "test_poda_api_service_module_ipp")
    monkeypatch.setattr(module.AddressAPIService, "_load_shape_rj", lambda self: None)
    service = module.AddressAPIService()

    class DummyResponse:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, *args, **kwargs):
            return DummyResponse(
                {
                    "candidates": [
                        {
                            "address": "Rua Alfa, Centro",
                            "attributes": {"cl": "111"},
                            "location": {"y": -22.9, "x": -43.2},
                        }
                    ]
                }
            )

        async def post(self, *args, **kwargs):
            return DummyResponse({"id": "20", "name": "Centro"})

    monkeypatch.setattr(module, "InterceptedHTTPClient", lambda **kwargs: DummyClient())
    monkeypatch.setattr(
        module, "jaro_similarity", lambda a, b: 0.2 if a == "Rua A" else 0.95
    )

    result = await service.get_ipp_street_code(
        logradouro_nome="Rua A",
        logradouro_nome_ipp="Rua IPP",
        bairro_nome_ipp="Centro",
        latitude=-22.9,
        longitude=-43.2,
    )

    assert result["logradouro_id"] == "111"
    assert result["bairro_nome"] == "Centro"
