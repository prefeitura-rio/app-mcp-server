"""
Testes da DEV-365: a busca do logradouro no IPP e a abertura do chamado no
SGRC, na poda de árvore, não mandam nome de rua, bairro nem o erro do SGRC
para o log.

Antes, `get_endereco_info` e `get_ipp_street_code` (`api_service.py`) logavam o
nome do bairro da geolocalização, o logradouro do IPP, a URL do geocode do IPP
(que leva o logradouro e o bairro na query), o candidato escolhido com o código
`cl`, o logradouro completo consultado e o bairro antes e depois da troca. No
`workflow.py`, `new_ticket` logava `str(exc)` da falha do SGRC e `_open_ticket`
chamava `logger.exception(exc)`, que leva a mensagem e o traceback: os dois
podem trazer o corpo do chamado (descrição, solicitante, endereço). A redação
global por padrão não alcança texto sem formato nem rótulo (CHATR-167).

Invariantes cobertos aqui:
- nenhuma linha de log da busca no IPP (similaridade alta, similaridade baixa
  com e sem bairro, bairro ausente, troca de bairro pelo SGRC) contém trecho do
  logradouro, do bairro ou do candidato sintéticos;
- o log registra só metadado: obtido sim/não, similaridade, distância;
- o fluxo não muda: a consulta ao IPP continua levando o logradouro e o bairro
  e o resultado devolvido é o mesmo;
- a falha do SGRC é logada só com o tipo da exceção, sem traceback, e a
  resposta ao agente continua a mesma.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[4]
API_SERVICE_PATH = (
    "src/tools/multi_step_service/workflows/poda_de_arvore/api/api_service.py"
)
WORKFLOW_PATH = "src/tools/multi_step_service/workflows/poda_de_arvore/workflow.py"
PODA_DIR = PROJECT_ROOT / "src" / "tools" / "multi_step_service" / "workflows"

# Dados sintéticos.
LOGRADOURO_GOOGLE = "Rua Quimera Sintetica"
LOGRADOURO_IPP = "123 - Rua Quimera Sintetica"
LOGRADOURO_IPP_DIFERENTE = "Travessa Zircônio Inventado"
BAIRRO_IPP = "Vila Brumal Ficticia"
BAIRRO_GOOGLE = "Jardim Ermelinda Quarzo"
BAIRRO_SGRC = "Parque Nebuloso Sigiloso"
CANDIDATO = f"{LOGRADOURO_GOOGLE}, {BAIRRO_SGRC}"
CODIGO_CL = 987654
LATITUDE = -22.9135791
LONGITUDE = -43.2468024
DESCRICAO_CHAMADO = "Poda na Rua Quimera Sintetica 4321, pedido de Zoraide Ficticia"

TRECHOS_PROIBIDOS = (
    "Quimera",
    "Sintetica",
    "Zircônio",
    "Inventado",
    "Brumal",
    "Ficticia",
    "Ermelinda",
    "Quarzo",
    "Nebuloso",
    "Sigiloso",
    "Zoraide",
    "4321",
    str(CODIGO_CL),
    "pgeo3",
    "Address=",
    "Traceback",
)


def _sem_endereco(texto: str) -> None:
    for trecho in TRECHOS_PROIBIDOS:
        assert trecho not in texto, f"dado do endereço ou do chamado no log: {trecho!r}"


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

    def mensagens(self, nivel: str) -> list:
        return [r[1] for r in self.registros if r[0] == nivel]

    def niveis(self) -> set:
        return {r[0] for r in self.registros}


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


# ---------------------------------------------------------------------------
# AddressAPIService: get_endereco_info e get_ipp_street_code
# ---------------------------------------------------------------------------


class _ClienteHTTPFake:
    """Substitui o `InterceptedHTTPClient`: guarda as URLs e responde fixo."""

    gets = []
    posts = []
    candidatos = []

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, url):
        _ClienteHTTPFake.gets.append(url)
        dados = {"candidates": list(_ClienteHTTPFake.candidatos)}
        return types.SimpleNamespace(json=lambda: dados)

    async def post(self, url, headers=None, content=None):
        _ClienteHTTPFake.posts.append(content)
        return types.SimpleNamespace(json=lambda: {"id": 42, "name": BAIRRO_SGRC})


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
        types.SimpleNamespace(InterceptedHTTPClient=_ClienteHTTPFake),
    )
    _ClienteHTTPFake.gets = []
    _ClienteHTTPFake.posts = []
    _ClienteHTTPFake.candidatos = [
        {
            "address": CANDIDATO,
            "location": {"x": LONGITUDE, "y": LATITUDE},
            "attributes": {"cl": CODIGO_CL},
        }
    ]

    modulo = _load_module("test_poda_api_service_dev365", API_SERVICE_PATH)
    capturado = _LoggerCapturado()
    monkeypatch.setattr(modulo, "logger", capturado)
    return modulo, capturado


@pytest.mark.asyncio
async def test_get_endereco_info_nao_loga_nome_do_bairro(api_service, monkeypatch):
    modulo, capturado = api_service
    servico = modulo.AddressAPIService()

    def nearest(latitude, longitude):
        return modulo.NearestLocation(
            id_logradouro=CODIGO_CL,
            name_logradouro=LOGRADOURO_IPP,
            id_bairro=77,
            name_bairro=BAIRRO_IPP,
        )

    monkeypatch.setattr(servico, "get_nearest_logradouro_and_bairro", nearest)

    resultado = await servico.get_endereco_info(latitude=LATITUDE, longitude=LONGITUDE)

    assert resultado["logradouro_nome"] == LOGRADOURO_IPP
    assert resultado["bairro_nome"] == BAIRRO_IPP
    assert resultado["bairro_id"] == "77"

    log = capturado.texto()
    _sem_endereco(log)
    assert "77" not in log
    assert "Bairro obtido pela geolocalização: sim" in log


@pytest.mark.asyncio
async def test_similaridade_alta_nao_loga_logradouro_nem_url(api_service):
    modulo, capturado = api_service

    resultado = await modulo.AddressAPIService().get_ipp_street_code(
        logradouro_nome=LOGRADOURO_GOOGLE,
        logradouro_nome_ipp=LOGRADOURO_IPP,
        bairro_nome_ipp=BAIRRO_IPP,
        latitude=LATITUDE,
        longitude=LONGITUDE,
    )

    # Comportamento anterior preservado: sem consulta e sem resultado.
    assert resultado is None
    assert _ClienteHTTPFake.gets == []

    log = capturado.texto()
    _sem_endereco(log)
    assert "Logradouro IPP obtido: sim" in log
    assert "Similaridade alta o suficiente: 1.0" in log


@pytest.mark.asyncio
async def test_sem_bairro_ipp_e_troca_de_bairro_logam_so_metadado(api_service):
    modulo, capturado = api_service

    resultado = await modulo.AddressAPIService().get_ipp_street_code(
        logradouro_nome=LOGRADOURO_GOOGLE,
        logradouro_nome_ipp=LOGRADOURO_IPP_DIFERENTE,
        bairro_nome_ipp=" ",
        latitude=LATITUDE,
        longitude=LONGITUDE,
        bairro_google=BAIRRO_GOOGLE,
    )

    # A consulta ao IPP continua com o logradouro e o bairro do Google.
    assert len(_ClienteHTTPFake.gets) == 1
    assert LOGRADOURO_GOOGLE in _ClienteHTTPFake.gets[0]
    assert BAIRRO_GOOGLE in _ClienteHTTPFake.gets[0]
    # E a troca de bairro continua consultando o SGRC pelo nome do candidato.
    assert _ClienteHTTPFake.posts == [f'{{"name": "{BAIRRO_SGRC}"}}']
    assert resultado == {
        "logradouro_id": CODIGO_CL,
        "logradouro_nome": LOGRADOURO_GOOGLE,
        "bairro_id": 42,
        "bairro_nome": BAIRRO_SGRC,
    }

    log = capturado.texto()
    _sem_endereco(log)
    assert "Consultando o geocode do IPP" in log
    assert "Logradouro no IPP mais próximo encontrado: sim, distância: 0 metros" in log
    assert "Bairro obtido anteriormente com geolocalização: não" in log
    assert "Bairro obtido agora com busca por similaridade: sim" in log


@pytest.mark.asyncio
async def test_similaridade_baixa_com_bairro_loga_so_metadado(api_service):
    modulo, capturado = api_service

    resultado = await modulo.AddressAPIService().get_ipp_street_code(
        logradouro_nome=LOGRADOURO_GOOGLE,
        logradouro_nome_ipp=LOGRADOURO_IPP_DIFERENTE,
        bairro_nome_ipp=BAIRRO_IPP,
        latitude=LATITUDE,
        longitude=LONGITUDE,
    )

    assert len(_ClienteHTTPFake.gets) == 1
    assert BAIRRO_IPP in _ClienteHTTPFake.gets[0]
    assert resultado["logradouro_id"] == CODIGO_CL
    assert resultado["bairro_nome"] == BAIRRO_SGRC

    log = capturado.texto()
    _sem_endereco(log)
    assert "Logradouro no IPP mais similar encontrado: sim, similaridade:" in log
    assert "Bairro obtido anteriormente com geolocalização: sim" in log


@pytest.mark.asyncio
async def test_bairro_ipp_ausente_com_similaridade_alta_loga_so_metadado(
    api_service,
):
    modulo, capturado = api_service

    resultado = await modulo.AddressAPIService().get_ipp_street_code(
        logradouro_nome=LOGRADOURO_GOOGLE,
        logradouro_nome_ipp=LOGRADOURO_IPP,
        bairro_nome_ipp=" ",
        latitude=LATITUDE,
        longitude=LONGITUDE,
        bairro_google=BAIRRO_GOOGLE,
    )

    # O endereço consultado passa a ser o logradouro do IPP com o bairro do Google.
    assert len(_ClienteHTTPFake.gets) == 1
    assert (
        "Address=Rua Quimera Sintetica, Jardim Ermelinda Quarzo&"
        in (_ClienteHTTPFake.gets[0])
    )
    assert resultado["bairro_nome"] == BAIRRO_SGRC

    log = capturado.texto()
    _sem_endereco(log)
    assert "Bairro IPP não identificado. Vou considerar o do Google." in log


# ---------------------------------------------------------------------------
# Workflow: new_ticket e _open_ticket
# ---------------------------------------------------------------------------


class SGRCBusinessRuleException(Exception):
    pass


class SGRCInvalidBodyException(Exception):
    pass


class SGRCMalformedBodyException(Exception):
    pass


class SGRCDuplicateTicketException(Exception):
    pass


class SGRCEquivalentTicketException(Exception):
    pass


class SGRCInternalErrorException(Exception):
    pass


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
        types.SimpleNamespace(
            build_ticket_payload=lambda state: ("endereco", "solicitante", "descricao")
        ),
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
            SGRCBusinessRuleException=SGRCBusinessRuleException,
            SGRCInvalidBodyException=SGRCInvalidBodyException,
            SGRCMalformedBodyException=SGRCMalformedBodyException,
            SGRCDuplicateTicketException=SGRCDuplicateTicketException,
            SGRCEquivalentTicketException=SGRCEquivalentTicketException,
            SGRCInternalErrorException=SGRCInternalErrorException,
        ),
    )

    modulo = _load_module("test_poda_workflow_dev365", WORKFLOW_PATH)
    capturado = _LoggerCapturado()
    monkeypatch.setattr(modulo, "logger", capturado)
    return modulo, capturado


def _estado(service_models):
    return service_models.ServiceState(
        user_id="u-sintetico", service_name="poda_de_arvore", payload={}, data={}
    )


@pytest.mark.asyncio
async def test_new_ticket_loga_so_o_tipo_do_erro_do_sgrc(poda, monkeypatch):
    modulo, capturado = poda

    async def async_new_ticket(**kwargs):
        raise SGRCInvalidBodyException(f"corpo recusado: {kwargs['description']}")

    monkeypatch.setattr(modulo, "async_new_ticket", async_new_ticket)
    workflow = modulo.PodaDeArvoreWorkflow(use_fake_api=False)

    with pytest.raises(SGRCInvalidBodyException):
        await workflow.new_ticket(
            classification_code="svc", description=DESCRICAO_CHAMADO
        )

    log = capturado.texto()
    _sem_endereco(log)
    [mensagem] = capturado.mensagens("error")
    assert mensagem.startswith("Erro ao criar ticket. Tempo: ")
    assert mensagem.endswith("s. Erro: SGRCInvalidBodyException")


@pytest.mark.parametrize(
    ("excecao", "error_code", "error_message"),
    [
        (
            SGRCBusinessRuleException(f"regra: {DESCRICAO_CHAMADO}"),
            "erro_interno",
            f"regra: {DESCRICAO_CHAMADO}",
        ),
        (
            SGRCMalformedBodyException(f"malformado: {DESCRICAO_CHAMADO}"),
            "erro_interno",
            f"malformado: {DESCRICAO_CHAMADO}",
        ),
        (
            ValueError(f"valor: {DESCRICAO_CHAMADO}"),
            "erro_interno",
            f"valor: {DESCRICAO_CHAMADO}",
        ),
        (
            SGRCDuplicateTicketException(f"duplicado: {DESCRICAO_CHAMADO}"),
            "erro_ticket_duplicado",
            f"duplicado: {DESCRICAO_CHAMADO}",
        ),
        (
            SGRCEquivalentTicketException(f"equivalente: {DESCRICAO_CHAMADO}"),
            "erro_ticket_duplicado",
            f"equivalente: {DESCRICAO_CHAMADO}",
        ),
        (
            SGRCInternalErrorException(f"interno: {DESCRICAO_CHAMADO}"),
            "erro_sgrc",
            "Sistema indisponível",
        ),
        (
            RuntimeError(f"inesperado: {DESCRICAO_CHAMADO}"),
            "erro_geral",
            f"inesperado: {DESCRICAO_CHAMADO}",
        ),
    ],
)
@pytest.mark.asyncio
async def test_open_ticket_loga_so_o_tipo_e_resposta_nao_muda(
    poda, service_models, monkeypatch, excecao, error_code, error_message
):
    modulo, capturado = poda

    async def new_ticket(**_kwargs):
        raise excecao

    workflow = modulo.PodaDeArvoreWorkflow(use_fake_api=False)
    monkeypatch.setattr(workflow, "new_ticket", new_ticket)

    resultado = await workflow._open_ticket(_estado(service_models))

    assert resultado.data["ticket_created"] is False
    assert resultado.data["error"] == error_code
    assert resultado.agent_response.error_message == error_message

    log = capturado.texto()
    _sem_endereco(log)
    # Sem `logger.exception`: nada de traceback com a mensagem da exceção.
    assert "exception" not in capturado.niveis()
    assert capturado.mensagens("error") == [
        f"Erro ao abrir ticket no SGRC: {type(excecao).__name__}"
    ]
