"""
Testes da DEV-354: a identificação do endereço na poda de árvore não manda o
endereço digitado pelo cidadão para o log.

Antes, `_collect_address` logava `Endereço identificado: {address_text}` e, na
falha, `Erro ao processar endereço: {e}`, cuja mensagem pode trazer o endereço
(o `httpx.HTTPStatusError` do geolocalizador cita a URL da requisição). No
`AddressAPIService` que o passo chama, `google_geolocator` logava o resultado
inteiro do Google (endereço formatado, CEP, coordenada) e a coordenada exata, e
os erros de `get_endereco_info` e `get_ipp_street_code` levavam `str(e)`. A
redação global por padrão não alcança endereço sem formato nem rótulo
(CHATR-167).

Invariantes cobertos aqui:
- nenhuma linha de log do passo (sucesso com a API fake, sucesso com o
  geolocalizador e o IPP stubados, erro HTTP do geolocalizador) contém trecho
  do endereço, do endereço formatado pelo Google ou das coordenadas;
- o sucesso registra só metadado: `resumir_texto_livre` do endereço e
  identificado sim; o erro registra só o tipo da exceção;
- o fluxo do passo não muda: o endereço segue no estado para a confirmação;
- o `google_geolocator` real (com só o cliente do Google Maps stubado) não loga
  endereço, CEP nem coordenada; os erros de `get_endereco_info` e de
  `get_ipp_street_code` levam só o tipo e nenhuma coordenada.
"""

import importlib.util
import sys
import types
from pathlib import Path

import httpx
import pytest

from src.utils.pii import resumir_texto_livre


PROJECT_ROOT = Path(__file__).resolve().parents[4]
WORKFLOW_PATH = "src/tools/multi_step_service/workflows/poda_de_arvore/workflow.py"
PODA_DIR = PROJECT_ROOT / "src" / "tools" / "multi_step_service" / "workflows"

# Dados sintéticos.
ENDERECO = "Rua Sintetica dos Ipes 4321 fundos da vila"
ENDERECO_FORMATADO = "R. Sintetica dos Ipes, 4321 - Rio de Janeiro - RJ"
LATITUDE = -22.9135791
LONGITUDE = -43.2468024
TRECHOS_PROIBIDOS = (
    "Sintetica",
    "Ipes",
    "4321",
    "fundos",
    "vila",
    "22.91",
    "43.24",
    "9135791",
    "2468024",
)


def _ensure_package(name: str, path: Path) -> None:
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(path)]
    sys.modules[name] = pkg


def _load_module(module_name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(
        module_name, PROJECT_ROOT / relative_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _LoggerCapturado:
    """Guarda toda chamada de log, em qualquer nível, com os argumentos."""

    def __init__(self):
        self.registros = []

    def __getattr__(self, nivel):
        def _registrar(msg, *args, **kwargs):
            self.registros.append((nivel, str(msg), repr(args), repr(kwargs)))

        return _registrar

    def texto(self) -> str:
        return "\n".join(" ".join(r) for r in self.registros)


@pytest.fixture
def service_models():
    return sys.modules["src.tools.multi_step_service.core.models"]


@pytest.fixture
def poda(monkeypatch):
    """Carrega o `workflow.py` da poda com as integrações externas stubadas."""
    _ensure_package("src", PROJECT_ROOT / "src")
    _ensure_package("src.config", PROJECT_ROOT / "src" / "config")
    _ensure_package("src.tools", PROJECT_ROOT / "src" / "tools")
    _ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")
    _ensure_package(
        "src.tools.multi_step_service",
        PROJECT_ROOT / "src" / "tools" / "multi_step_service",
    )
    _ensure_package(
        "src.tools.multi_step_service.core",
        PROJECT_ROOT / "src" / "tools" / "multi_step_service" / "core",
    )
    _ensure_package("src.tools.multi_step_service.workflows", PODA_DIR)
    _ensure_package(
        "src.tools.multi_step_service.workflows.poda_de_arvore.api",
        PODA_DIR / "poda_de_arvore" / "api",
    )

    monkeypatch.setitem(
        sys.modules, "src.config.env", types.SimpleNamespace(PODA_SERVICE_ID="svc")
    )
    monkeypatch.setitem(
        sys.modules,
        "src.tools.multi_step_service.core.base_workflow",
        types.SimpleNamespace(
            BaseWorkflow=type("BaseWorkflow", (), {"__init__": lambda self: None}),
            handle_errors=lambda func: func,
        ),
    )
    templates = _load_module(
        "src.tools.multi_step_service.workflows.poda_de_arvore.templates",
        "src/tools/multi_step_service/workflows/poda_de_arvore/templates.py",
    )
    monkeypatch.setitem(
        sys.modules,
        "src.tools.multi_step_service.workflows.poda_de_arvore",
        types.SimpleNamespace(templates=templates),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.tools.multi_step_service.workflows.poda_de_arvore.integrations",
        types.SimpleNamespace(build_ticket_payload=lambda *a, **k: None),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.tools.multi_step_service.workflows.poda_de_arvore.api.api_service",
        types.SimpleNamespace(
            SGRCAPIService=lambda: types.SimpleNamespace(),
            AddressAPIService=lambda: types.SimpleNamespace(),
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
    monkeypatch.setitem(
        sys.modules,
        "prefeitura_rio.integrations.sgrc.exceptions",
        types.SimpleNamespace(
            SGRCBusinessRuleException=Exception,
            SGRCInvalidBodyException=Exception,
            SGRCMalformedBodyException=Exception,
            SGRCDuplicateTicketException=Exception,
            SGRCEquivalentTicketException=Exception,
            SGRCInternalErrorException=Exception,
        ),
    )

    modulo = _load_module("test_poda_workflow_dev354", WORKFLOW_PATH)
    capturado = _LoggerCapturado()
    monkeypatch.setattr(modulo, "logger", capturado)
    return modulo, capturado


def _estado(service_models):
    return service_models.ServiceState(
        user_id="u-sintetico",
        service_name="poda_de_arvore",
        payload={"address": ENDERECO},
    )


def _sem_endereco(texto: str) -> None:
    for trecho in TRECHOS_PROIBIDOS:
        assert trecho not in texto, f"trecho do endereço no log: {trecho!r}"


@pytest.mark.asyncio
async def test_sucesso_com_api_fake_loga_so_metadado(poda, service_models):
    modulo, capturado = poda
    workflow = modulo.PodaDeArvoreWorkflow(use_fake_api=True)

    resultado = await workflow._collect_address(_estado(service_models))

    assert resultado.agent_response is None
    assert resultado.data["address_needs_confirmation"] is True
    assert resultado.data["address_temp"]["original_text"] == ENDERECO

    log = capturado.texto()
    _sem_endereco(log)
    assert f"Endereço identificado: sim, {resumir_texto_livre(ENDERECO)}" in log


@pytest.mark.asyncio
async def test_sucesso_com_geolocalizador_e_ipp_nao_loga_endereco(poda, service_models):
    modulo, capturado = poda
    workflow = modulo.PodaDeArvoreWorkflow(use_fake_api=False)
    chamadas = []

    async def google_geolocator(endereco):
        chamadas.append(endereco)
        return {
            "valid": True,
            "logradouro": "Rua Sintetica dos Ipes",
            "numero": "4321",
            "bairro": "Bairro Ficticio",
            "latitude": LATITUDE,
            "longitude": LONGITUDE,
            "formatted_address": ENDERECO_FORMATADO,
        }

    async def get_endereco_info(**_kwargs):
        return {
            "logradouro_id": "999",
            "logradouro_nome": "Rua Sintetica dos Ipes",
            "bairro_id": "77",
            "bairro_nome": "Bairro Ficticio",
        }

    workflow.address_service = types.SimpleNamespace(
        google_geolocator=google_geolocator, get_endereco_info=get_endereco_info
    )

    resultado = await workflow._collect_address(_estado(service_models))

    assert chamadas == [f"{ENDERECO}, Rio de Janeiro - RJ"]
    assert resultado.agent_response is None
    assert resultado.data["address_temp"]["formatted_address"] == ENDERECO_FORMATADO

    log = capturado.texto()
    _sem_endereco(log)
    assert "Endereço identificado: sim" in log


@pytest.mark.asyncio
async def test_erro_do_geolocalizador_loga_so_o_tipo(poda, service_models):
    modulo, capturado = poda
    workflow = modulo.PodaDeArvoreWorkflow(use_fake_api=False)

    async def google_geolocator(endereco):
        request = httpx.Request(
            "GET", "https://maps.local/geocode/json", params={"address": endereco}
        )
        response = httpx.Response(400, request=request, json={})
        response.raise_for_status()

    workflow.address_service = types.SimpleNamespace(
        google_geolocator=google_geolocator
    )

    resultado = await workflow._collect_address(_estado(service_models))

    assert resultado.agent_response is not None
    assert resultado.data["address_validation"]["attempts"] == 1

    log = capturado.texto()
    _sem_endereco(log)
    erros = [r for r in capturado.registros if r[0] == "error"]
    assert [r[1] for r in erros] == ["Erro ao processar endereço: HTTPStatusError"]


# ---------------------------------------------------------------------------
# AddressAPIService: o geolocalizador e o IPP que `_collect_address` chama.
# ---------------------------------------------------------------------------

CEP = "20999-123"
TRECHOS_PROIBIDOS_API = TRECHOS_PROIBIDOS + ("20999", "999-123")
COORDENADAS = ("22.91", "43.24", "9135791", "2468024")

GEOCODE_GOOGLE = [
    {
        "formatted_address": f"{ENDERECO_FORMATADO}, {CEP}",
        "address_components": [
            {"long_name": "4321", "types": ["street_number"]},
            {"long_name": "Rua Sintetica dos Ipes", "types": ["route"]},
            {"long_name": "Bairro Ficticio", "types": ["sublocality_level_1"]},
            {"long_name": CEP, "types": ["postal_code"]},
            {
                "long_name": "Rio de Janeiro",
                "types": ["administrative_area_level_2"],
            },
            {
                "long_name": "Rio de Janeiro",
                "short_name": "RJ",
                "types": ["administrative_area_level_1"],
            },
        ],
        "geometry": {"location": {"lat": LATITUDE, "lng": LONGITUDE}},
    }
]


class _SessaoFake:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _ClienteGoogleFake:
    enderecos = []

    def __init__(self, _session, key):
        self.key = key

    async def geocode(self, endereco):
        _ClienteGoogleFake.enderecos.append(endereco)
        return GEOCODE_GOOGLE


@pytest.fixture
def api_service(monkeypatch):
    """Carrega o `api_service.py` real só com o HTTP externo stubado."""
    _ensure_package("src", PROJECT_ROOT / "src")
    _ensure_package("src.config", PROJECT_ROOT / "src" / "config")
    _ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")
    env_module = types.SimpleNamespace(
        CHATBOT_INTEGRATIONS_URL="https://integrations.local/",
        CHATBOT_INTEGRATIONS_KEY="chave-sintetica",
        GMAPS_API_TOKEN="token-sintetico",
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
        sys.modules, "aiohttp", types.SimpleNamespace(ClientSession=_SessaoFake)
    )
    monkeypatch.setitem(
        sys.modules,
        "async_googlemaps",
        types.SimpleNamespace(AsyncClient=_ClienteGoogleFake),
    )
    _ClienteGoogleFake.enderecos = []

    modulo = _load_module(
        "test_poda_api_service_dev354",
        "src/tools/multi_step_service/workflows/poda_de_arvore/api/api_service.py",
    )
    capturado = _LoggerCapturado()
    monkeypatch.setattr(modulo, "logger", capturado)
    return modulo, capturado


@pytest.mark.asyncio
async def test_google_geolocator_real_nao_loga_endereco_cep_nem_coordenada(
    api_service,
):
    modulo, capturado = api_service

    resultado = await modulo.AddressAPIService().google_geolocator(
        f"{ENDERECO}, Rio de Janeiro - RJ"
    )

    assert _ClienteGoogleFake.enderecos == [f"{ENDERECO}, Rio de Janeiro - RJ"]
    assert resultado["valid"] is True
    assert resultado["latitude"] == LATITUDE
    assert resultado["cep"] == CEP

    log = capturado.texto()
    for trecho in TRECHOS_PROIBIDOS_API:
        assert trecho not in log, f"trecho do endereço no log: {trecho!r}"
    assert "GEOCODE RESULT: 1 resultado(s)" in log


@pytest.mark.asyncio
async def test_get_endereco_info_loga_so_o_tipo_dos_erros(api_service, monkeypatch):
    modulo, capturado = api_service
    servico = modulo.AddressAPIService()

    def nearest_falha(latitude, longitude):
        raise ValueError(f"sem vizinho para {latitude}, {longitude}")

    async def ipp_falha(**_kwargs):
        raise RuntimeError(f"falhou para {ENDERECO}")

    monkeypatch.setattr(servico, "get_nearest_logradouro_and_bairro", nearest_falha)
    monkeypatch.setattr(servico, "get_ipp_street_code", ipp_falha)

    resultado = await servico.get_endereco_info(
        latitude=LATITUDE,
        longitude=LONGITUDE,
        logradouro_google="Rua Sintetica dos Ipes",
    )

    assert resultado["abertura_manual"] is True
    log = capturado.texto()
    for trecho in TRECHOS_PROIBIDOS_API:
        assert trecho not in log, f"trecho do endereço no log: {trecho!r}"
    assert (
        "Falha ao obter endereço usando get_nearest_logradouro_and_bairro(): ValueError"
    ) in log
    assert "Erro ao processar endereço: RuntimeError" in log


@pytest.mark.asyncio
async def test_get_ipp_street_code_nao_loga_coordenada_e_erro_so_com_tipo(
    api_service, monkeypatch
):
    modulo, capturado = api_service

    class _ClienteIPP:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, _url):
            # Candidato sem vírgula: nenhum é escolhido e o fluxo cai no except.
            candidato = {
                "address": "Rua Sem Bairro",
                "location": {"x": LONGITUDE, "y": LATITUDE},
                "attributes": {"cl": 1},
            }
            return types.SimpleNamespace(json=lambda: {"candidates": [candidato]})

    monkeypatch.setattr(modulo, "InterceptedHTTPClient", _ClienteIPP)

    resultado = await modulo.AddressAPIService().get_ipp_street_code(
        logradouro_nome="Avenida Qualquer",
        logradouro_nome_ipp="Travessa Diferente",
        bairro_nome_ipp=" ",
        latitude=LATITUDE,
        longitude=LONGITUDE,
    )

    assert "error" in resultado
    log = capturado.texto()
    for trecho in COORDENADAS:
        assert trecho not in log, f"coordenada no log: {trecho!r}"
    assert "Erro ao buscar código IPP: AttributeError" in log
