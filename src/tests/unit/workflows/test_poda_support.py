import importlib.util
import json
import random
import sys
import threading
import time
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


class FakeSTRtree:
    """
    Stub de `shapely.STRtree` para o módulo de API da poda.

    Com `shapely.wkt.loads` stubado para identidade, as geometrias chegam como
    os próprios números do JSON de fixture e `Point(x, y)` vira a tupla `(x, y)`
    — então o vizinho mais próximo é a menor diferença absoluta para a longitude.

    Serve para os testes que só precisam de um vizinho previsível (cache,
    `to_thread`). Ele *não* prova que a integração com o shapely de verdade
    está certa — quem cobre isso é
    `test_get_nearest_bate_com_a_varredura_linear_anterior`, que roda com
    `stub_shapely=False`.
    """

    def __init__(self, geometries):
        self._geometries = list(geometries)

    def nearest(self, point):
        longitude = point[0]
        return min(
            range(len(self._geometries)),
            key=lambda i: abs(self._geometries[i] - longitude),
        )


def prepare_poda_api_module(
    monkeypatch, module_name="test_poda_api_service_module", stub_shapely=True
):
    """Carrega `api_service.py` com as dependências pesadas stubadas.

    `stub_shapely=False` deixa o shapely de verdade no lugar. Isso era
    impossível enquanto ele chegava de carona pelo geopandas; agora que o
    `pyproject` o declara direto, o teste de paridade pode exercitar o
    `STRtree` real em vez de um dublê que reimplementa a busca.
    """
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
    if stub_shapely:
        monkeypatch.setitem(
            sys.modules,
            "shapely",
            types.SimpleNamespace(STRtree=FakeSTRtree),
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


def _escrever_fixtures_de_geometria(tmp_path):
    """Dois arquivos no formato que `get_nearest_logradouro_and_bairro` espera."""
    (tmp_path / "logradouros.json").write_text(
        json.dumps(
            [
                {"id": 1, "nome": "Rua Longe", "geometry": -50.0},
                {"id": 2, "nome": "Rua Perto", "geometry": -43.2},
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "bairros.json").write_text(
        json.dumps(
            [
                {"id": 20, "nome": "Centro", "geometry": -43.19},
                {"id": 30, "nome": "Bangu", "geometry": -43.46},
            ]
        ),
        encoding="utf-8",
    )


def test_get_nearest_usa_indice_espacial_e_devolve_o_mais_proximo(
    monkeypatch, tmp_path
):
    module = prepare_poda_api_module(monkeypatch, "test_poda_api_service_module_near")
    monkeypatch.setattr(module.env, "DATA_DIR", tmp_path)
    _escrever_fixtures_de_geometria(tmp_path)

    resultado = module.AddressAPIService().get_nearest_logradouro_and_bairro(
        latitude=-22.9, longitude=-43.2
    )

    assert resultado.id_logradouro == 2
    assert resultado.name_logradouro == "Rua Perto"
    assert resultado.id_bairro == 20
    assert resultado.name_bairro == "Centro"


def test_get_nearest_le_cada_arquivo_uma_unica_vez(monkeypatch, tmp_path):
    """
    O conteúdo é imutável em runtime: reler e reparsear a cada consulta era o
    custo linear que o cache de processo elimina.

    A prova vem do `cache_info()` do próprio cache, e não de um espião em
    `pd.read_json`: `module.pd` é o pandas de verdade, então patchá-lo trocaria
    a função no processo inteiro para provar um fato local deste módulo.
    """
    module = prepare_poda_api_module(monkeypatch, "test_poda_api_service_module_cache")
    monkeypatch.setattr(module.env, "DATA_DIR", tmp_path)
    _escrever_fixtures_de_geometria(tmp_path)

    service = module.AddressAPIService()
    service.get_nearest_logradouro_and_bairro(latitude=-22.9, longitude=-43.2)
    service.get_nearest_logradouro_and_bairro(latitude=-22.8, longitude=-43.4)

    # Duas consultas x dois arquivos = quatro acessos; só os dois primeiros
    # tocam o disco.
    info = module._build_geometry_index.cache_info()
    assert (info.misses, info.hits) == (2, 2)


def _fixtures_wkt_reais(tmp_path, quantidade=40):
    """Fixtures com WKT de verdade, para rodar contra o shapely real.

    Os ids são espaçados e fora de ordem de propósito. O `STRtree` devolve um
    índice *posicional*, enquanto a varredura linear que ele substituiu
    indexava por rótulo (`.loc[idxmin()]`); com ids distintos de suas posições,
    um desalinhamento entre a árvore e o DataFrame aparece como id trocado em
    vez de passar despercebido.
    """
    rng = random.Random(20260828)

    def registros(prefixo, base_id):
        itens = []
        for i in range(quantidade):
            longitude = -43.8 + rng.random() * 0.9
            latitude = -23.05 + rng.random() * 0.35
            itens.append(
                {
                    "id": base_id + i * 7,
                    "nome": f"{prefixo} {i}",
                    "geometry": (
                        f"LINESTRING ({longitude} {latitude}, "
                        f"{longitude + 0.004} {latitude + 0.004})"
                    ),
                }
            )
        return itens

    (tmp_path / "logradouros.json").write_text(
        json.dumps(registros("Rua", 1000)), encoding="utf-8"
    )
    (tmp_path / "bairros.json").write_text(
        json.dumps(registros("Bairro", 5000)), encoding="utf-8"
    )


# Pontos espalhados pela caixa das fixtures, incluindo dois fora dela, para
# que o vizinho mais próximo não seja sempre o mesmo registro.
CONSULTAS_DE_PARIDADE = [
    (-22.90, -43.20),
    (-22.95, -43.45),
    (-23.00, -43.70),
    (-22.88, -43.35),
    (-22.70, -43.10),
    (-23.30, -43.90),
]


def test_get_nearest_bate_com_a_varredura_linear_anterior(monkeypatch, tmp_path):
    """Paridade com o `idxmin` que o `STRtree` substituiu, no shapely real.

    Os outros testes deste bloco rodam com `FakeSTRtree`, que reimplementa a
    busca — eles validam o dublê. O que pode quebrar em produção é a integração
    de verdade: se o índice devolvido pela árvore não corresponder à linha do
    DataFrame, a consulta devolve o logradouro errado sem levantar erro algum.
    """
    import shapely.wkt
    from shapely.geometry import Point

    module = prepare_poda_api_module(
        monkeypatch, "test_poda_api_service_module_paridade", stub_shapely=False
    )
    monkeypatch.setattr(module.env, "DATA_DIR", tmp_path)
    _fixtures_wkt_reais(tmp_path)

    def varredura_linear(nome_arquivo, ponto):
        """A implementação anterior, ponta a ponta."""
        registros = module.pd.read_json(tmp_path / nome_arquivo)
        registros["geometry"] = registros["geometry"].apply(shapely.wkt.loads)
        distancias = registros["geometry"].apply(ponto.distance)
        return registros.loc[distancias.idxmin()]

    service = module.AddressAPIService()

    for latitude, longitude in CONSULTAS_DE_PARIDADE:
        resultado = service.get_nearest_logradouro_and_bairro(latitude, longitude)
        ponto = Point(longitude, latitude)

        esperado_logradouro = varredura_linear("logradouros.json", ponto)
        esperado_bairro = varredura_linear("bairros.json", ponto)

        assert (resultado.id_logradouro, resultado.name_logradouro) == (
            esperado_logradouro["id"],
            esperado_logradouro["nome"],
        ), f"logradouro divergiu em {latitude}, {longitude}"
        assert (resultado.id_bairro, resultado.name_bairro) == (
            esperado_bairro["id"],
            esperado_bairro["nome"],
        ), f"bairro divergiu em {latitude}, {longitude}"


def test_indice_e_construido_uma_unica_vez_sob_concorrencia(monkeypatch, tmp_path):
    """O cold start não pode construir o índice N vezes em paralelo.

    `lru_cache` só grava o resultado no fim: sozinho, ele não impede que N
    threads entrem juntas na função e montem N índices completos, dos quais um
    sobrevive. Como o índice é construído de dentro de `asyncio.to_thread`,
    esse é exatamente o cenário de uma rajada de requisições no pod novo.
    """
    module = prepare_poda_api_module(
        monkeypatch, "test_poda_api_service_module_concorrencia"
    )
    monkeypatch.setattr(module.env, "DATA_DIR", tmp_path)
    _escrever_fixtures_de_geometria(tmp_path)

    # O parse é o que custa caro no arquivo real; aqui ele fica lento de
    # propósito, para que as threads se sobreponham de fato dentro da função.
    parses = []
    loads_original = module.loads

    def loads_lento(valor):
        parses.append(valor)
        time.sleep(0.02)
        return loads_original(valor)

    monkeypatch.setattr(module, "loads", loads_lento)

    QUANTIDADE_DE_THREADS = 8
    largada = threading.Barrier(QUANTIDADE_DE_THREADS)
    falhas = []

    def consultar():
        try:
            largada.wait(timeout=10)
            module._geometry_index("logradouros.json")
        except Exception as exc:  # pragma: no cover - só falha se houver bug
            falhas.append(exc)

    threads = [threading.Thread(target=consultar) for _ in range(QUANTIDADE_DE_THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not falhas
    assert module._build_geometry_index.cache_info().misses == 1
    # Duas geometrias na fixture. Sem o lock seriam 2 x 8.
    assert len(parses) == 2


@pytest.mark.asyncio
async def test_get_endereco_info_nao_bloqueia_o_event_loop(monkeypatch, tmp_path):
    """A consulta é síncrona e lê disco: precisa sair do loop via `to_thread`."""
    module = prepare_poda_api_module(monkeypatch, "test_poda_api_service_module_thread")
    monkeypatch.setattr(module.env, "DATA_DIR", tmp_path)
    _escrever_fixtures_de_geometria(tmp_path)

    service = module.AddressAPIService()
    thread_da_consulta = {}
    consulta_original = service.get_nearest_logradouro_and_bairro

    def registrando_thread(latitude, longitude):
        thread_da_consulta["nome"] = threading.current_thread()
        return consulta_original(latitude, longitude)

    monkeypatch.setattr(
        service, "get_nearest_logradouro_and_bairro", registrando_thread
    )

    async def fake_get_ipp_street_code(**kwargs):
        return {"logradouro_id": "99", "bairro_nome": "Centro"}

    monkeypatch.setattr(service, "get_ipp_street_code", fake_get_ipp_street_code)

    resultado = await service.get_endereco_info(-22.9, -43.2, "Rua A", "Centro")

    assert resultado["logradouro_id"] == "99"
    assert thread_da_consulta["nome"] is not threading.current_thread()


@pytest.mark.asyncio
async def test_address_service_get_ipp_street_code(monkeypatch):
    module = prepare_poda_api_module(monkeypatch, "test_poda_api_service_module_ipp")
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
