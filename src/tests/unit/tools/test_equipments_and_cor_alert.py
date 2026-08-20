import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest
from google.api_core.exceptions import BadRequest, GoogleAPIError
from google.cloud import bigquery


PROJECT_ROOT = Path(__file__).resolve().parents[4]


def load_module(module_name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(
        module_name, PROJECT_ROOT / relative_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def ensure_package(name: str, path: Path):
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(path)]
    sys.modules[name] = pkg
    return pkg


def passthrough_interceptor(*_args, **_kwargs):
    def decorator(func):
        return func

    return decorator


class FakeBigQueryTimeoutError(TimeoutError):
    """Espelha `src.utils.bigquery.BigQueryTimeoutError`, inclusive a base.

    A base importa: o escalonamento de `except` em `pluscode_service` depende
    de o tipo de prazo não ser capturado pelos ramos de baixo. O teste
    `test_tipos_de_excecao_espelham_o_modulo_real` prende esta cópia à
    definição de verdade.
    """


class FakeBigQueryQueryError(Exception):
    """Espelha `src.utils.bigquery.BigQueryQueryError`."""


@pytest.mark.asyncio
async def test_cor_alert_normalization_geocode_and_create(monkeypatch):
    ensure_package("src", PROJECT_ROOT / "src")
    ensure_package("src.tools", PROJECT_ROOT / "src" / "tools")
    ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")
    ensure_package("src.config", PROJECT_ROOT / "src" / "config")

    saved_alerts = []
    queued_alerts = []

    monkeypatch.setitem(
        sys.modules,
        "src.utils.bigquery",
        types.SimpleNamespace(
            save_cor_alert_in_bq_background=lambda **kwargs: asyncio.sleep(
                0, result=saved_alerts.append(kwargs)
            ),
            save_cor_alert_to_queue_background=lambda **kwargs: asyncio.sleep(
                0, result=queued_alerts.append(kwargs)
            ),
            get_datetime=lambda: "2026-04-08T10:00:00.000000",
        ),
    )
    env_module = types.SimpleNamespace(
        ENVIRONMENT="test",
        GOOGLE_MAPS_API_URL="https://maps.googleapis.com/maps/api/geocode/json",
        GOOGLE_MAPS_API_KEY="google-key",
    )
    monkeypatch.setitem(sys.modules, "src.config.env", env_module)
    monkeypatch.setitem(
        sys.modules, "src.config", types.SimpleNamespace(env=env_module)
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.log",
        types.SimpleNamespace(
            logger=types.SimpleNamespace(
                info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None
            )
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.error_interceptor",
        types.SimpleNamespace(interceptor=passthrough_interceptor),
    )

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, params=None):
            if "latlng" in params:
                return types.SimpleNamespace(
                    raise_for_status=lambda: None,
                    json=lambda: {
                        "status": "OK",
                        "results": [
                            {
                                "address_components": [
                                    {
                                        "long_name": "Jd America",
                                        "types": ["sublocality_level_1"],
                                    }
                                ]
                            }
                        ],
                    },
                )
            return types.SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "status": "OK",
                    "results": [
                        {
                            "formatted_address": "Rua A, Rio de Janeiro - RJ",
                            "geometry": {"location": {"lat": -22.9, "lng": -43.2}},
                            "address_components": [],
                        }
                    ],
                },
            )

    monkeypatch.setitem(
        sys.modules,
        "src.utils.http_client",
        types.SimpleNamespace(InterceptedHTTPClient=FakeClient),
    )

    module = load_module("test_cor_alert_tools_module", "src/tools/cor_alert_tools.py")

    assert module._normalize_text("  Jd Ámerica  ") == "jd america"
    assert module.normalize_neighborhood("Jd America") == "jardim america"
    assert (
        module._extract_google_neighborhood(
            {
                "address_components": [
                    {"long_name": "Acari", "types": ["neighborhood"]},
                ]
            }
        )
        == "Acari"
    )

    coords = await module.get_coordinates_google("Rua A, 10")
    assert coords["provider"] == "Google Maps"

    coords = await module.geocode_address("Rua A, 10")
    assert coords["bairro_normalizado"] == "jardim america"

    result = await module.create_cor_alert("", "alagamento", "alta", "desc", "Rua A")
    assert result["success"] is False

    result = await module.create_cor_alert("u1", "invalido", "alta", "desc", "Rua A")
    assert result["success"] is False

    result = await module.create_cor_alert("u1", "alagamento", "alta", "desc", "Rua A")
    assert result["success"] is True
    assert saved_alerts
    assert queued_alerts

    queued_alerts.clear()
    result = await module.create_cor_alert("u1", "alagamento", "baixa", "desc", "Rua A")
    assert result["success"] is True
    assert queued_alerts == []


@pytest.mark.asyncio
async def test_equipments_tools_instructions_and_whitelist(monkeypatch):
    ensure_package("src", PROJECT_ROOT / "src")
    ensure_package("src.tools", PROJECT_ROOT / "src" / "tools")
    ensure_package(
        "src.tools.equipments", PROJECT_ROOT / "src" / "tools" / "equipments"
    )
    ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")
    ensure_package("src.config", PROJECT_ROOT / "src" / "config")

    created_tasks = []

    def fake_disparar(coro, nome=None):
        created_tasks.append(coro)
        coro.close()
        return None

    # Ver `src/utils/background.py`: as tools disparam a escrita por este helper
    # justamente para a task não ser coletada antes de rodar.
    monkeypatch.setitem(
        sys.modules,
        "src.utils.background",
        types.SimpleNamespace(disparar_em_background=fake_disparar),
    )

    monkeypatch.setitem(
        sys.modules,
        "src.tools.equipments.pluscode_service",
        types.SimpleNamespace(
            get_category_equipments=lambda: asyncio.sleep(0, result={"cats": ["A"]}),
            get_tematic_instructions_for_equipments=lambda: asyncio.sleep(
                0, result={"ok": True}
            ),
            get_pluscode_coords_equipments=lambda address, categories=None: (
                asyncio.sleep(
                    0,
                    result=[{"nome": "Equip", "categoria": "CF", "esfera": "ESTADUAL"}],
                )
            ),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.bigquery",
        types.SimpleNamespace(
            save_response_in_bq_background=lambda **kwargs: asyncio.sleep(0)
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.error_interceptor",
        types.SimpleNamespace(interceptor=passthrough_interceptor),
    )
    env_module = types.SimpleNamespace(
        EQUIPMENTS_VALID_THEMES=["saude", "assistencia"],
        GOOGLE_MAPS_API_URL="https://maps.googleapis.com/maps/api/geocode/json",
        GOOGLE_MAPS_API_KEY="google-key",
    )
    monkeypatch.setitem(sys.modules, "src.config.env", env_module)
    monkeypatch.setitem(
        sys.modules, "src.config", types.SimpleNamespace(env=env_module)
    )

    monkeypatch.setitem(
        sys.modules,
        "src.tools.equipments.utils",
        types.SimpleNamespace(
            get_coords_from_google_maps_api=lambda address: {
                "lat": -22.9,
                "lng": -43.2,
                "bairro_normalizado": "outro-bairro",
            }
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.tools.cor_alert_tools",
        types.SimpleNamespace(
            _extract_google_neighborhood=lambda result: "Acari",
            normalize_neighborhood=lambda value: "acari",
        ),
    )

    class FakeSyncClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def get_sync(self, url, params=None):
            return types.SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "status": "OK",
                    "results": [{"address_components": []}],
                },
            )

    monkeypatch.setitem(
        sys.modules,
        "src.utils.http_client",
        types.SimpleNamespace(InterceptedHTTPClient=FakeSyncClient),
    )

    module = load_module(
        "test_equipments_tools_module", "src/tools/equipments_tools.py"
    )

    assert module.get_valid_themes() == ["saude", "assistencia"]
    assert (
        "retorne todos os equipamentos"
        in module.get_instructions_for_equipments([]).lower()
    )
    assert (
        "governo do estado do rio de janeiro"
        in module.get_instructions_for_equipments(
            [{"categoria": "CF", "esfera": "ESTADUAL"}]
        ).lower()
    )
    assert (
        "ponto de apoio"
        in module.get_instructions_for_equipments(
            [{"categoria": "PONTOS_DE_APOIO"}]
        ).lower()
    )
    assert "agendamento prévio" in module.get_instructions_for_equipments(
        [{"categoria": "CRAS"}]
    )

    monkeypatch.setattr(
        module,
        "get_equipments",
        lambda address, categories=None: asyncio.sleep(
            0, result=[{"nome": "Equip", "categoria": "CF", "esfera": "ESTADUAL"}]
        ),
    )
    result = await module.get_equipments_with_instructions("Rua A", categories=["CF"])
    assert result["equipamentos"][0]["nome"] == "Equip"
    assert "governo do estado do rio de janeiro" in result["instructions"].lower()

    result = await module.get_equipments_with_instructions(
        "Rua A", categories=["PONTOS_DE_APOIO"]
    )
    assert "defesa civil" in result["instructions"].lower()

    monkeypatch.setitem(
        sys.modules,
        "src.tools.equipments.utils",
        types.SimpleNamespace(
            get_coords_from_google_maps_api=lambda address: {
                "lat": -22.9,
                "lng": -43.2,
                "bairro_normalizado": "bairro-nao-permitido",
            }
        ),
    )
    module = load_module(
        "test_equipments_tools_module_blocked", "src/tools/equipments_tools.py"
    )
    monkeypatch.setattr(
        module,
        "get_equipments",
        lambda address, categories=None: asyncio.sleep(0, result=[]),
    )
    result = await module.get_equipments_with_instructions(
        "Rua A", categories=["PONTOS_DE_APOIO"]
    )
    assert result["equipamentos"] == []
    assert "199" in result["instructions"]

    result = await module.get_equipments_categories()
    assert result == {"cats": ["A"]}
    assert created_tasks


def _load_pluscode_service(monkeypatch, get_bigquery_result, plus8_coords=(None, None)):
    """Carrega `pluscode_service` com as dependências externas trocadas.

    `plus8_coords` é o par devolvido por `get_plus8_coords_from_address`. O
    default `(None, None)` mantém o atalho usado pelos testes de instruções
    temáticas, que não chegam a montar a query de equipamentos.
    """
    ensure_package("src", PROJECT_ROOT / "src")
    ensure_package("src.tools", PROJECT_ROOT / "src" / "tools")
    ensure_package(
        "src.tools.equipments", PROJECT_ROOT / "src" / "tools" / "equipments"
    )
    ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")

    monkeypatch.setitem(
        sys.modules,
        "src.tools.equipments.utils",
        types.SimpleNamespace(
            get_plus8_coords_from_address=lambda address: plus8_coords
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.bigquery",
        types.SimpleNamespace(
            get_bigquery_result=get_bigquery_result,
            BigQueryTimeoutError=FakeBigQueryTimeoutError,
            BigQueryQueryError=FakeBigQueryQueryError,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.error_interceptor",
        types.SimpleNamespace(interceptor=passthrough_interceptor),
    )
    return load_module(
        "test_pluscode_service_module", "src/tools/equipments/pluscode_service.py"
    )


def _capturar_logs(monkeypatch, module):
    """Troca o logger do módulo por coletores separados de nível.

    O nível faz parte do contrato do escalonamento de exceções: prazo estourado
    é `warning` (degradação prevista, vale repetir) e falha inesperada é
    `exception` (pede traceback e investigação). Um coletor único não
    distinguiria os dois.
    """
    logs = types.SimpleNamespace(error=[], warning=[], exception=[])
    monkeypatch.setattr(
        module,
        "logger",
        types.SimpleNamespace(
            error=logs.error.append,
            warning=logs.warning.append,
            exception=logs.exception.append,
            info=lambda *_a, **_k: None,
        ),
    )
    return logs


def _capturar_erros(monkeypatch, module):
    """Coletor só do nível ERROR, para os testes que não olham os outros."""
    return _capturar_logs(monkeypatch, module).error


@pytest.mark.asyncio
async def test_instrucoes_degradam_quando_a_tabela_externa_cai(monkeypatch):
    """CHATR-119: perder a Google Sheet não pode derrubar o fluxo inteiro.

    O erro real é um 400 do BigQuery — não um `NotFound` —, então ele
    atravessa a degradação de `get_bigquery_result` e chegaria à tool.
    """

    async def bigquery_quebrado(query, query_parameters=None, **_kwargs):
        raise BadRequest(
            "400 Error while reading table: "
            "rj-iplanrio.plus_codes.equipamentos_instrucoes, error message: "
            "Spreadsheet not found. File: 1VPnJSf9puDgZ-Ed9MRkpe3Jy38nKxGLp7O9-ydAdm98"
        )

    module = _load_pluscode_service(monkeypatch, bigquery_quebrado)
    erros = _capturar_erros(monkeypatch, module)

    resultado = await module.get_tematic_instructions_for_equipments(tema="geral")

    assert isinstance(resultado, list) and len(resultado) == 1
    assert resultado[0]["error"] == "Instruções temporariamente indisponíveis"
    # O contrato com o agente: seguir para `equipments_by_address` mesmo assim.
    assert "equipments_by_address" in resultado[0]["message"]
    # Falha conhecida de infraestrutura: não pode ser logada como bug.
    assert len(erros) == 1
    assert "Tabela externa" in erros[0] and "INESPERADO" not in erros[0]


@pytest.mark.asyncio
async def test_instrucoes_degradam_tambem_em_erro_inesperado(monkeypatch):
    """Um bug nosso não pode chegar ao cidadão, mas tem de gritar no log.

    Mesmo fallback do caso esperado; o que muda é a mensagem de ERROR, para
    que planilha fora do ar e defeito de código não se confundam na busca.
    """

    async def bigquery_com_bug(query, query_parameters=None, **_kwargs):
        raise RuntimeError("boom")

    module = _load_pluscode_service(monkeypatch, bigquery_com_bug)
    erros = _capturar_erros(monkeypatch, module)

    resultado = await module.get_tematic_instructions_for_equipments(tema="geral")

    assert resultado[0]["error"] == "Instruções temporariamente indisponíveis"
    assert "equipments_by_address" in resultado[0]["message"]
    assert len(erros) == 1
    assert "INESPERADO" in erros[0]
    # `{e!r}` preserva o tipo do erro no log — é o que aponta para o bug.
    assert "RuntimeError" in erros[0]


@pytest.mark.asyncio
async def test_instrucoes_retornam_os_dados_quando_a_tabela_responde(monkeypatch):
    chamadas = []

    async def bigquery_ok(query, query_parameters=None, **kwargs):
        chamadas.append((query, query_parameters, kwargs))
        return [{"tema": "educacao", "instrucoes": "..."}]

    module = _load_pluscode_service(monkeypatch, bigquery_ok)

    resultado = await module.get_tematic_instructions_for_equipments(tema="educacao")

    assert resultado == [{"tema": "educacao", "instrucoes": "..."}]
    query, parametros, kwargs = chamadas[0]
    # O tema vai por parâmetro do BigQuery, nunca interpolado no texto do SQL.
    assert "educacao" not in query
    assert [(p.name, p.value) for p in parametros] == [("tema", "educacao")]
    # CHATR-115: a chave de cache é semântica, e o tema faz parte da identidade
    # da consulta — sem ele, todos os temas dividiriam a mesma entrada.
    assert kwargs["cache_namespace"] == "equipments_instructions"
    assert kwargs["cache_key_parts"] == {"tema": "educacao"}


# ---------------------------------------------------------------------------
# CHATR-117 — `get_pluscode_coords_equipments`
#
# É a única query do serviço que carrega dados vindos da chamada do agente: o
# endereço (virado plus8 + coordenadas) e a lista de categorias. O filtro de
# categorias já foi montado por f-string interpolada direto na cláusula `IN`;
# os testes abaixo existem para que voltar àquele formato quebre o CI.
# ---------------------------------------------------------------------------

_PLUS8 = "589R2QCH"
_COORDS = {"lat": -22.9035, "lng": -43.2096}

# Hostis de propósito. Hoje `categories` vem de uma lista controlada, mas o dia
# em que a origem afrouxar, é este valor que aparece no SQL se a parametrização
# for desfeita — contra uma service account com escopo `cloud-platform`.
_CATEGORIAS_HOSTIS = ["SAUDE') OR 1=1 --", "EDUCACAO"]


def _pluscode_capturando_query(monkeypatch, dados=None, plus8_coords=(_PLUS8, _COORDS)):
    """Carrega o serviço com um `get_bigquery_result` que grava o que recebeu."""
    chamadas = []

    async def bigquery_ok(query=None, query_parameters=None, **_kwargs):
        chamadas.append((query, query_parameters))
        return [] if dados is None else dados

    module = _load_pluscode_service(monkeypatch, bigquery_ok, plus8_coords)
    return module, chamadas


def _por_nome(parametros):
    return {p.name: p for p in parametros}


@pytest.mark.asyncio
async def test_categorias_vao_por_parametro_e_nunca_no_texto_do_sql(monkeypatch):
    """CHATR-117: categorias são parâmetro nomeado, não interpolação de string.

    O assert que carrega o teste é o negativo: nenhum pedaço dos valores pode
    aparecer no SQL enviado. É ele que falha se a f-string voltar.
    """
    module, chamadas = _pluscode_capturando_query(monkeypatch)

    await module.get_pluscode_coords_equipments("Rua A", categories=_CATEGORIAS_HOSTIS)

    query, parametros = chamadas[0]
    assert "OR 1=1" not in query
    assert "EDUCACAO" not in query

    categorias = _por_nome(parametros)["categories"]
    assert isinstance(categorias, bigquery.ArrayQueryParameter)
    assert categorias.array_type == "STRING"
    assert list(categorias.values) == _CATEGORIAS_HOSTIS


@pytest.mark.asyncio
async def test_filtro_de_categorias_entra_nas_duas_ctes(monkeypatch):
    """O placeholder aparece duas vezes (grid e território) e ambas contam.

    Trocar só a primeira ocorrência deixaria o território sem filtro: resultado
    errado, não erro de sintaxe — portanto invisível sem este assert.
    """
    module, chamadas = _pluscode_capturando_query(monkeypatch)

    await module.get_pluscode_coords_equipments("Rua A", categories=["SAUDE"])

    query, _ = chamadas[0]
    assert query.count("UNNEST(@categories)") == 2
    assert "__replace_categories__" not in query


@pytest.mark.asyncio
@pytest.mark.parametrize("sem_categorias", [None, []])
async def test_sem_categorias_nao_sobra_placeholder_nem_parametro(
    monkeypatch, sem_categorias
):
    """Sem filtro o placeholder tem de sumir — ele é sintaxe inválida no BigQuery."""
    module, chamadas = _pluscode_capturando_query(monkeypatch)

    await module.get_pluscode_coords_equipments("Rua A", categories=sem_categorias)

    query, parametros = chamadas[0]
    assert "__replace_categories__" not in query
    assert "UNNEST(@categories)" not in query
    assert "categories" not in _por_nome(parametros)


@pytest.mark.asyncio
async def test_plus8_e_coordenadas_tambem_vao_por_parametro(monkeypatch):
    """O endereço do cidadão vira plus8/coordenadas — que também não são interpolados."""
    module, chamadas = _pluscode_capturando_query(monkeypatch)

    await module.get_pluscode_coords_equipments("Rua A")

    query, parametros = chamadas[0]
    por_nome = _por_nome(parametros)
    assert (por_nome["plus8"].type_, por_nome["plus8"].value) == ("STRING", _PLUS8)
    assert (por_nome["longitude"].type_, por_nome["longitude"].value) == (
        "FLOAT64",
        _COORDS["lng"],
    )
    assert (por_nome["latitude"].type_, por_nome["latitude"].value) == (
        "FLOAT64",
        _COORDS["lat"],
    )
    assert _PLUS8 not in query
    assert str(_COORDS["lng"]) not in query


@pytest.mark.asyncio
async def test_retorno_devolve_inputs_coords_plus8_e_dados(monkeypatch):
    """O contrato com a tool: os dados vêm acompanhados do que os originou."""
    linhas = [{"nome_oficial": "UPA Copacabana", "distancia_metros": 120}]
    module, _ = _pluscode_capturando_query(monkeypatch, dados=linhas)

    resultado = await module.get_pluscode_coords_equipments(
        "Rua A", categories=["SAUDE"]
    )

    assert resultado == {
        "inputs": {"address": "Rua A", "categories": ["SAUDE"]},
        "coords": _COORDS,
        "plus8": _PLUS8,
        "data": linhas,
    }


@pytest.mark.asyncio
async def test_falha_do_bigquery_vira_dict_de_erro_e_nao_excecao(monkeypatch):
    """O tool call não pode estourar: o agente recebe a falha como dado."""

    async def bigquery_quebrado(query=None, query_parameters=None, **_kwargs):
        raise BadRequest("400 Error while reading table")

    module = _load_pluscode_service(monkeypatch, bigquery_quebrado, (_PLUS8, _COORDS))
    erros = _capturar_erros(monkeypatch, module)

    resultado = await module.get_pluscode_coords_equipments("Rua A")

    assert resultado["error"] == "Erro no request do bigquery"
    assert "400" in resultado["message"]
    assert len(erros) == 1


# ---------------------------------------------------------------------------
# Escalonamento de exceções em `get_pluscode_coords_equipments`
#
# O CHATR-125 criou `BigQueryTimeoutError` e `BigQueryQueryError` para separar
# prazo, infraestrutura e bug nosso. O único chamador capturava só `Exception`
# e achatava os três num dict idêntico, então a distinção existia no tipo e
# morria na fronteira. Os testes abaixo prendem cada degrau ao seu desfecho.
# ---------------------------------------------------------------------------


def _pluscode_que_falha(monkeypatch, erro):
    async def bigquery_quebrado(query=None, query_parameters=None, **_kwargs):
        raise erro

    module = _load_pluscode_service(monkeypatch, bigquery_quebrado, (_PLUS8, _COORDS))
    return module, _capturar_logs(monkeypatch, module)


@pytest.mark.asyncio
async def test_timeout_do_bigquery_tem_desfecho_proprio(monkeypatch):
    """Prazo estourado é transitório: mensagem própria e log em WARNING.

    Se caísse no ramo genérico, o agente receberia "erro no request" para uma
    condição que quase sempre resolve na próxima tentativa — e a operação veria
    um ERROR de bug para o que é, na verdade, carga.
    """
    module, logs = _pluscode_que_falha(
        monkeypatch, FakeBigQueryTimeoutError("timed out after 10.0s")
    )

    resultado = await module.get_pluscode_coords_equipments("Rua A")

    assert resultado["error"] == "Consulta ao BigQuery excedeu o tempo limite"
    assert "10.0s" in resultado["message"]
    assert len(logs.warning) == 1 and "Timeout" in logs.warning[0]
    # Degradação prevista não pode poluir o canal de bug.
    assert logs.error == [] and logs.exception == []


@pytest.mark.asyncio
async def test_erro_inesperado_da_leitura_pede_investigacao(monkeypatch):
    """`BigQueryQueryError` é "não era prazo nem falha conhecida" — vai de exception."""
    module, logs = _pluscode_que_falha(
        monkeypatch, FakeBigQueryQueryError("Failed to execute BigQuery query: boom")
    )

    resultado = await module.get_pluscode_coords_equipments("Rua A")

    assert resultado["error"] == "Erro inesperado na consulta ao BigQuery"
    assert "boom" in resultado["message"]
    # `logger.exception` leva o traceback, que é a única pista do que houve.
    assert len(logs.exception) == 1 and "INESPERADO" in logs.exception[0]
    assert logs.warning == [] and logs.error == []


@pytest.mark.asyncio
async def test_falha_fora_do_bigquery_ainda_vira_dado(monkeypatch):
    """O último degrau: nada pode estourar o tool call, nem bug nosso."""
    module, logs = _pluscode_que_falha(monkeypatch, RuntimeError("bug na montagem"))

    resultado = await module.get_pluscode_coords_equipments("Rua A")

    assert resultado["error"] == "Erro inesperado ao buscar equipamentos"
    assert "bug na montagem" in resultado["message"]
    assert len(logs.exception) == 1


@pytest.mark.asyncio
async def test_cada_falha_tem_um_desfecho_distinguivel(monkeypatch):
    """O que o escalonamento entrega: quatro causas, quatro respostas.

    Sem este assert, um `except` fora de ordem (o genérico antes do específico)
    passaria despercebido — todos os casos continuariam devolvendo um dict, só
    que sempre o mesmo.
    """
    causas = [
        FakeBigQueryTimeoutError("prazo"),
        BadRequest("400 tabela externa"),
        FakeBigQueryQueryError("inesperado"),
        RuntimeError("bug"),
    ]
    vistos = []
    for causa in causas:
        module, _logs = _pluscode_que_falha(monkeypatch, causa)
        resultado = await module.get_pluscode_coords_equipments("Rua A")
        vistos.append(resultado["error"])

    assert len(set(vistos)) == len(causas), vistos


def test_dubles_de_excecao_tem_as_bases_de_que_o_escalonamento_depende():
    """Prende os dublês locais à hierarquia que a ordem dos `except` assume.

    Os testes acima usam cópias (o módulo real é substituído por stub no
    loader). A ordem dos ramos só está correta enquanto os tipos forem
    disjuntos: se `BigQueryTimeoutError` virasse subclasse de `GoogleAPIError`,
    o ramo de infraestrutura passaria a engolir o de prazo. O lado real da
    mesma garantia está em
    `src/tests/unit/utils/test_bigquery_exceptions.py::test_hierarquia_mantem_os_tipos_disjuntos`.
    """
    assert issubclass(FakeBigQueryTimeoutError, TimeoutError)
    assert issubclass(FakeBigQueryQueryError, Exception)
    for duble in (FakeBigQueryTimeoutError, FakeBigQueryQueryError):
        assert not issubclass(duble, GoogleAPIError)
    assert not issubclass(FakeBigQueryQueryError, TimeoutError)


# ---------------------------------------------------------------------------
# `get_category_equipments`
#
# É o terceiro call site do cache e o único cujo namespace não leva nenhuma
# parte semântica — a chave inteira seria o namespace se não fosse o
# fingerprint do SQL. Também é o único sem `try/except`: a falha sobe até a
# tool. Nada disso tinha teste.
# ---------------------------------------------------------------------------


def _pluscode_categorias(monkeypatch, dados=None, erro=None):
    chamadas = []

    async def bigquery(query=None, query_parameters=None, **kwargs):
        chamadas.append((query, query_parameters, kwargs))
        if erro is not None:
            raise erro
        return dados or []

    module = _load_pluscode_service(monkeypatch, bigquery)
    return module, chamadas


@pytest.mark.asyncio
async def test_categorias_agrupadas_por_secretaria(monkeypatch):
    """O contrato com a tool: linhas planas viram dict secretaria -> categorias."""
    module, _chamadas = _pluscode_categorias(
        monkeypatch,
        dados=[
            {"secretaria_responsavel": "SMS", "categoria": "CF"},
            {"secretaria_responsavel": "SMS", "categoria": "CMS"},
            {"secretaria_responsavel": "SMASDH", "categoria": "CRAS"},
        ],
    )

    resultado = await module.get_category_equipments()

    assert resultado == {"SMS": ["CF", "CMS"], "SMASDH": ["CRAS"]}


@pytest.mark.asyncio
async def test_categorias_usam_namespace_proprio_sem_partes(monkeypatch):
    """CHATR-115: a query não tem parâmetro, então o namespace a identifica.

    Sem `cache_key_parts` a chave depende só do namespace mais o fingerprint do
    SQL — é justamente o caso que o fingerprint existe para proteger. Passar um
    namespace repetido aqui faria esta consulta dividir entrada com outra.
    """
    module, chamadas = _pluscode_categorias(monkeypatch)

    await module.get_category_equipments()

    query, parametros, kwargs = chamadas[0]
    assert kwargs["cache_namespace"] == "equipments_categories"
    assert "cache_key_parts" not in kwargs
    assert parametros is None
    # A query é estática: nada vindo de fora entra no texto.
    assert "@" not in query


@pytest.mark.asyncio
async def test_categorias_sem_linhas_devolvem_dict_vazio(monkeypatch):
    """Zero linhas é resposta legítima, não erro — e não pode estourar no loop."""
    module, _chamadas = _pluscode_categorias(monkeypatch, dados=[])

    assert await module.get_category_equipments() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "erro",
    [
        FakeBigQueryTimeoutError("timed out after 10.0s"),
        BadRequest("400 Error while reading table"),
    ],
    ids=["timeout", "falha-conhecida"],
)
async def test_falha_nas_categorias_sobe_com_o_tipo_original(monkeypatch, erro):
    """Documenta o comportamento real: aqui a falha *propaga*.

    Diferente de `get_pluscode_coords_equipments`, esta função não converte a
    falha em dado — e `get_equipments_categories`, que a chama, também não
    trata. O teste não aprova nem reprova a escolha; ele prende o tipo que
    chega ao chamador, para que uma mudança de contrato seja deliberada.
    """
    module, _chamadas = _pluscode_categorias(monkeypatch, erro=erro)

    with pytest.raises(type(erro)):
        await module.get_category_equipments()


@pytest.mark.asyncio
async def test_endereco_sem_coordenadas_nao_chega_ao_bigquery(monkeypatch):
    """Sem geocodificação não há query: a falha vem antes de gastar BigQuery."""
    module, chamadas = _pluscode_capturando_query(
        monkeypatch, plus8_coords=(None, None)
    )

    with pytest.raises(module.GeocodingError, match="No coords found"):
        await module.get_pluscode_coords_equipments("Endereço inexistente")

    assert chamadas == []


@pytest.mark.asyncio
@pytest.mark.parametrize("plus8_vazio", [None, ""], ids=["none", "string-vazia"])
async def test_plus8_vazio_com_coords_nao_estoura_unbound_local(
    monkeypatch, plus8_vazio
):
    """O ramo negativo do guarda `if plus8:`, que existia mas não era tratado.

    Com coordenadas mas sem plus8, `query`, `latitude` e `longitude` nunca eram
    ligados e a montagem dos parâmetros estourava `UnboundLocalError` — *fora*
    do `try`, portanto sem virar o dict de erro que a tool sabe devolver. Uma
    falha de geocodificação chegava ao agente como defeito de código.
    """
    module, chamadas = _pluscode_capturando_query(
        monkeypatch, plus8_coords=(plus8_vazio, _COORDS)
    )

    with pytest.raises(module.GeocodingError) as exc:
        await module.get_pluscode_coords_equipments("Rua A")

    assert not isinstance(exc.value, UnboundLocalError)
    assert "Rua A" in str(exc.value)
    assert chamadas == []


@pytest.mark.asyncio
async def test_geocoding_error_continua_capturavel_como_exception(monkeypatch):
    """Tipo novo não pode quebrar quem já capturava largo."""
    module, _chamadas = _pluscode_capturando_query(
        monkeypatch, plus8_coords=(None, None)
    )

    assert issubclass(module.GeocodingError, Exception)
    with pytest.raises(Exception, match="No coords found"):
        await module.get_pluscode_coords_equipments("Endereço inexistente")


@pytest.mark.asyncio
async def test_geocodificacao_sai_do_event_loop(monkeypatch):
    """CHATR-102: a metade da função que ainda travava o loop.

    `get_plus8_coords_from_address` faz HTTP **bloqueante** (o
    `InterceptedHTTPClient` é criado com `sync=True, timeout=10.0`). Chamada
    direto de dentro da corrotina, congelava o loop pela duração da resposta
    do Google Maps — medido antes da correção: 1s de geocodificação, 1,02s de
    loop parado. E isso roda em toda chamada, inclusive nas que depois acertam
    o cache do BigQuery.

    O heartbeat é a medida: se o loop parar, o intervalo entre as batidas
    salta para a duração inteira da geocodificação.
    """
    import time

    atraso = 0.4

    def geocoder_bloqueante(address):
        time.sleep(atraso)  # o real é uma chamada HTTP síncrona
        return _PLUS8, _COORDS

    module, _chamadas = _pluscode_capturando_query(monkeypatch)
    monkeypatch.setattr(module, "get_plus8_coords_from_address", geocoder_bloqueante)

    batidas = []

    async def heartbeat():
        while True:
            batidas.append(time.monotonic())
            await asyncio.sleep(0.02)

    hb = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)
    await module.get_pluscode_coords_equipments("Rua A")
    await asyncio.sleep(0.05)  # deixa registrar a batida pós-geocodificação
    hb.cancel()

    intervalos = [b - a for a, b in zip(batidas, batidas[1:])]
    assert max(intervalos) < atraso / 2, max(intervalos)


@pytest.mark.asyncio
async def test_geocodificacao_recebe_o_endereco_por_nome(monkeypatch):
    """`run_in_executor` não repassa kwargs — daí o `functools.partial`.

    Sem ele, o endereço iria posicional e a assinatura nomeada quebraria em
    silêncio na primeira mudança de ordem dos parâmetros.
    """
    recebidos = []

    def geocoder(address):
        recebidos.append(address)
        return _PLUS8, _COORDS

    module, _chamadas = _pluscode_capturando_query(monkeypatch)
    monkeypatch.setattr(module, "get_plus8_coords_from_address", geocoder)

    await module.get_pluscode_coords_equipments("Rua B, 100")

    assert recebidos == ["Rua B, 100"]


def test_openlocationcode_roundtrip_and_helpers():
    openlocationcode = load_module(
        "test_openlocationcode_module", "src/tools/equipments/openlocationcode.py"
    )

    code = openlocationcode.encode(47.36559, 8.524997)
    decoded = openlocationcode.decode(code)
    short = openlocationcode.shorten(code, 47.5, 8.5)

    assert openlocationcode.isValid(code)
    assert openlocationcode.isShort(short)
    assert openlocationcode.recoverNearest(short, 47.4, 8.6) == code
    assert abs(decoded.latitudeCenter - 47.36559) < 0.001
    assert abs(decoded.longitudeCenter - 8.524997) < 0.001
    assert openlocationcode.normalizeLongitude(190) == -170
