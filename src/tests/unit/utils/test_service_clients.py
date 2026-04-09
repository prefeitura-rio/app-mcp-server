import asyncio
import base64
import importlib.util
import json
import sys
import types
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest


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


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error", request=types.SimpleNamespace(), response=self
            )


@pytest.mark.asyncio
async def test_rmi_oauth2_token_manager_and_helpers(monkeypatch):
    ensure_package("src", PROJECT_ROOT / "src")
    ensure_package("src.config", PROJECT_ROOT / "src" / "config")
    ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")

    env_module = types.SimpleNamespace(
        RMI_OAUTH_ISSUER="https://issuer.example",
        RMI_OAUTH_CLIENT_ID="client-id",
        RMI_OAUTH_CLIENT_SECRET="client-secret",
        RMI_OAUTH_SCOPES="scope-1",
    )
    monkeypatch.setitem(sys.modules, "src.config.env", env_module)
    monkeypatch.setitem(
        sys.modules, "src.config", types.SimpleNamespace(env=env_module)
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.error_interceptor",
        types.SimpleNamespace(interceptor=passthrough_interceptor),
    )

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, data=None, headers=None):
            self.calls.append((url, data, headers))
            return FakeResponse(
                200,
                {"access_token": "token-123", "expires_in": 3600},
            )

    fake_client = FakeClient()
    monkeypatch.setitem(
        sys.modules,
        "src.utils.http_client",
        types.SimpleNamespace(InterceptedHTTPClient=lambda **kwargs: fake_client),
    )
    monkeypatch.setitem(
        sys.modules,
        "loguru",
        types.SimpleNamespace(
            logger=types.SimpleNamespace(
                info=lambda *_a, **_k: None, error=lambda *_a, **_k: None
            )
        ),
    )

    module = load_module("test_rmi_oauth2_module", "src/utils/rmi_oauth2.py")
    manager = module.OAuth2TokenManager()

    token = await manager.get_access_token()
    assert token == "token-123"
    assert fake_client.calls[0][0].endswith("/protocol/openid-connect/token")

    fake_client.calls.clear()
    token_cached = await manager.get_access_token()
    assert token_cached == "token-123"
    assert fake_client.calls == []

    module._token_manager = None
    monkeypatch.setattr(
        module, "get_rmi_access_token", lambda: asyncio.sleep(0, result="abc")
    )
    assert await module.get_authorization_header() == "Bearer abc"
    assert module.is_oauth2_configured() is True


@pytest.mark.asyncio
async def test_rmi_oauth2_request_token_error_paths(monkeypatch):
    ensure_package("src", PROJECT_ROOT / "src")
    ensure_package("src.config", PROJECT_ROOT / "src" / "config")
    ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")

    env_module = types.SimpleNamespace(
        RMI_OAUTH_ISSUER="https://issuer.example",
        RMI_OAUTH_CLIENT_ID="client-id",
        RMI_OAUTH_CLIENT_SECRET="client-secret",
        RMI_OAUTH_SCOPES="scope-1",
    )
    monkeypatch.setitem(sys.modules, "src.config.env", env_module)
    monkeypatch.setitem(
        sys.modules, "src.config", types.SimpleNamespace(env=env_module)
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.error_interceptor",
        types.SimpleNamespace(interceptor=passthrough_interceptor),
    )
    monkeypatch.setitem(
        sys.modules,
        "loguru",
        types.SimpleNamespace(
            logger=types.SimpleNamespace(
                info=lambda *_a, **_k: None, error=lambda *_a, **_k: None
            )
        ),
    )

    class ErrorClient:
        def __init__(self, response=None, side_effect=None):
            self.response = response
            self.side_effect = side_effect

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, data=None, headers=None):
            if self.side_effect:
                raise self.side_effect
            return self.response

    monkeypatch.setitem(
        sys.modules,
        "src.utils.http_client",
        types.SimpleNamespace(
            InterceptedHTTPClient=lambda **kwargs: ErrorClient(
                response=FakeResponse(500, text="boom")
            )
        ),
    )
    module = load_module("test_rmi_oauth2_module_errors", "src/utils/rmi_oauth2.py")
    manager = module.OAuth2TokenManager()
    with pytest.raises(Exception, match="OAuth2 token request failed: 500"):
        await manager._request_token()

    monkeypatch.setitem(
        sys.modules,
        "src.utils.http_client",
        types.SimpleNamespace(
            InterceptedHTTPClient=lambda **kwargs: ErrorClient(
                response=FakeResponse(200, {"expires_in": 3600})
            )
        ),
    )
    module = load_module("test_rmi_oauth2_module_missing", "src/utils/rmi_oauth2.py")
    manager = module.OAuth2TokenManager()
    with pytest.raises(Exception, match="missing access_token"):
        await manager._request_token()

    monkeypatch.setitem(
        sys.modules,
        "src.utils.http_client",
        types.SimpleNamespace(
            InterceptedHTTPClient=lambda **kwargs: ErrorClient(
                side_effect=httpx.RequestError("network down")
            )
        ),
    )
    module = load_module("test_rmi_oauth2_module_network", "src/utils/rmi_oauth2.py")
    manager = module.OAuth2TokenManager()
    with pytest.raises(Exception, match="network down"):
        await manager._request_token()


@pytest.mark.asyncio
async def test_typesense_api_search_and_by_id(monkeypatch):
    ensure_package("src", PROJECT_ROOT / "src")
    ensure_package("src.config", PROJECT_ROOT / "src" / "config")
    ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")

    env_module = types.SimpleNamespace(
        TYPESENSE_HUB_SEARCH_URL="https://typesense.example/search"
    )
    monkeypatch.setitem(sys.modules, "src.config.env", env_module)
    monkeypatch.setitem(
        sys.modules, "src.config", types.SimpleNamespace(env=env_module)
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.error_interceptor",
        types.SimpleNamespace(interceptor=passthrough_interceptor),
    )

    class FakeAsyncClient:
        def __init__(self, timeout=None):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, params=None, headers=None):
            if url.endswith("/123"):
                return FakeResponse(
                    200,
                    {
                        "id": "123",
                        "nome_servico": "Serviço",
                        "resumo": "Resumo",
                        "tempo_atendimento": "2 dias",
                        "custo_servico": "Sem custo",
                        "resultado_solicitacao": "Resultado",
                        "descricao_completa": "Detalhe",
                        "documentos_necessarios": ["Doc"],
                        "instrucoes_solicitante": "Faça isso",
                        "servico_nao_cobre": "Não cobre",
                        "publico_especifico": ["Todos"],
                    },
                )
            return FakeResponse(
                200,
                {
                    "results": [
                        {
                            "title": "Título",
                            "id": "123",
                            "description": "Desc",
                            "category": "cat",
                            "metadata": {
                                "agents": {"tool_hint": "hint"},
                                "custo_servico": "0",
                                "descricao_completa": "full",
                                "is_free": True,
                                "orgao_gestor": ["Org"],
                                "publico_especifico": ["Todos"],
                                "documentos_necessarios": ["Doc"],
                                "instrucoes_solicitante": "Instr",
                                "legislacao_relacionada": ["Lei"],
                                "resultado_solicitacao": "Res",
                                "resumo_plaintext": "Resumo",
                                "servico_nao_cobre": "Nada",
                                "tempo_atendimento": "1 dia",
                                "score_info": {"x": 1},
                                "ai_score": {"y": 2},
                            },
                        }
                    ]
                },
            )

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    module = load_module("test_typesense_api_module", "src/utils/typesense_api.py")

    with pytest.raises(ValueError):
        module.HubSearchRequest()

    result = await module.hub_search(module.HubSearchRequest(q="iptu"))
    assert result["results_clean"][0]["hint"] == "hint"
    assert result["results_clean"][0]["title"] == "Título"

    result = await module.hub_search_by_id(module.HubSearchRequest(id="123"))
    assert result["id"] == "123"
    assert result["title"] == "Serviço"


def test_bigquery_helpers(monkeypatch):
    ensure_package("src", PROJECT_ROOT / "src")
    ensure_package("src.config", PROJECT_ROOT / "src" / "config")
    ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")

    env_module = types.SimpleNamespace(
        GCP_SERVICE_ACCOUNT_CREDENTIALS=base64.b64encode(
            json.dumps({"project_id": "proj-1"}).encode("utf-8")
        ).decode("utf-8"),
        GOOGLE_BIGQUERY_PAGE_SIZE=10,
    )
    monkeypatch.setitem(sys.modules, "src.config.env", env_module)
    monkeypatch.setitem(
        sys.modules, "src.config", types.SimpleNamespace(env=env_module)
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.error_interceptor",
        types.SimpleNamespace(interceptor=passthrough_interceptor),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.log",
        types.SimpleNamespace(
            logger=types.SimpleNamespace(
                info=lambda *_a, **_k: None,
                error=lambda *_a, **_k: None,
                warning=lambda *_a, **_k: None,
                exception=lambda *_a, **_k: None,
            )
        ),
    )

    module = load_module("test_bigquery_module", "src/utils/bigquery.py")
    fake_credentials = types.SimpleNamespace(
        project_id="proj-1",
        with_scopes=lambda scopes: types.SimpleNamespace(
            project_id="proj-1", scopes=scopes
        ),
    )
    bigquery_stub = types.SimpleNamespace(
        Client=lambda credentials, project: types.SimpleNamespace(
            credentials=credentials, project=project
        ),
        SchemaField=lambda *args, **kwargs: ("schema", args, kwargs),
        LoadJobConfig=lambda **kwargs: types.SimpleNamespace(**kwargs),
        TimePartitioning=lambda **kwargs: types.SimpleNamespace(**kwargs),
        TimePartitioningType=types.SimpleNamespace(DAY="DAY"),
        SchemaUpdateOption=types.SimpleNamespace(
            ALLOW_FIELD_ADDITION="ALLOW_FIELD_ADDITION"
        ),
    )
    not_found = type("FakeNotFound", (Exception,), {})
    monkeypatch.setattr(
        module.service_account.Credentials,
        "from_service_account_info",
        lambda info: fake_credentials,
    )
    monkeypatch.setattr(module, "bigquery", bigquery_stub)
    monkeypatch.setattr(module, "NotFound", not_found)

    creds = module.get_gcp_credentials(["scope-a"])
    assert creds.project_id == "proj-1"
    client = module.get_bigquery_client()
    assert client.project == "proj-1"
    assert "T" in module.get_datetime()

    class FakeRow(dict):
        def items(self):
            return super().items()

    class FakeQueryJob:
        def result(self, page_size=None):
            return [
                FakeRow(
                    {
                        "dt": datetime(2026, 4, 8, 10, 0, 0),
                        "d": date(2026, 4, 8),
                        "x": 1,
                    }
                )
            ]

    query_client = types.SimpleNamespace(query=lambda query: FakeQueryJob())
    monkeypatch.setattr(module, "get_bigquery_client", lambda: query_client)
    rows = module.get_bigquery_result("select 1")
    assert rows[0]["dt"].startswith("2026-04-08T10:00:00")
    assert rows[0]["d"] == "2026-04-08"

    class MissingClient:
        def query(self, query):
            raise not_found("missing")

    monkeypatch.setattr(module, "get_bigquery_client", lambda: MissingClient())
    assert module.get_bigquery_result("select 1") == []

    class ErrorClient:
        def query(self, query):
            raise RuntimeError("boom")

    monkeypatch.setattr(module, "get_bigquery_client", lambda: ErrorClient())
    with pytest.raises(Exception, match="Failed to execute BigQuery query"):
        module.get_bigquery_result("select 1")


def test_bigquery_save_functions(monkeypatch):
    ensure_package("src", PROJECT_ROOT / "src")
    ensure_package("src.config", PROJECT_ROOT / "src" / "config")
    ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")

    env_module = types.SimpleNamespace(
        GCP_SERVICE_ACCOUNT_CREDENTIALS=base64.b64encode(
            json.dumps({"project_id": "proj-1"}).encode("utf-8")
        ).decode("utf-8"),
        GOOGLE_BIGQUERY_PAGE_SIZE=10,
        ENVIRONMENT="test",
    )
    monkeypatch.setitem(sys.modules, "src.config.env", env_module)
    monkeypatch.setitem(
        sys.modules, "src.config", types.SimpleNamespace(env=env_module)
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.error_interceptor",
        types.SimpleNamespace(interceptor=passthrough_interceptor),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.log",
        types.SimpleNamespace(
            logger=types.SimpleNamespace(
                info=lambda *_a, **_k: None,
                error=lambda *_a, **_k: None,
                warning=lambda *_a, **_k: None,
                exception=lambda *_a, **_k: None,
            )
        ),
    )

    module = load_module("test_bigquery_savers_module", "src/utils/bigquery.py")
    bigquery_stub = types.SimpleNamespace(
        SchemaField=lambda *args, **kwargs: ("schema", args, kwargs),
        LoadJobConfig=lambda **kwargs: types.SimpleNamespace(**kwargs),
        TimePartitioning=lambda **kwargs: types.SimpleNamespace(**kwargs),
        TimePartitioningType=types.SimpleNamespace(DAY="DAY"),
        SchemaUpdateOption=types.SimpleNamespace(
            ALLOW_FIELD_ADDITION="ALLOW_FIELD_ADDITION"
        ),
    )
    monkeypatch.setattr(module, "bigquery", bigquery_stub)

    saved_payloads = []

    class FakeJob:
        def result(self):
            return None

    class FakeClient:
        def load_table_from_json(self, payload, table_name, job_config=None):
            saved_payloads.append((payload, table_name, job_config))
            return FakeJob()

    monkeypatch.setattr(module, "get_bigquery_client", lambda: FakeClient())
    monkeypatch.setattr(module, "get_datetime", lambda: "2026-04-08T10:00:00.000000")

    module.save_response_in_bq(
        data={"ok": True},
        endpoint="equipments",
        dataset_id="dataset",
        table_id="responses",
        project_id="proj-x",
        environment="staging",
    )
    payload, table_name, _job_config = saved_payloads[-1]
    assert table_name == "proj-x.dataset.responses"
    assert payload[0]["environment"] == "staging"
    assert payload[0]["data_particao"] == "2026-04-08"

    module.save_feedback_in_bq(
        user_id="u1",
        feedback="ótimo",
        timestamp="2026-04-08T12:34:56",
        environment="prod",
        dataset_id="dataset",
        table_id="feedback",
        project_id="proj-y",
    )
    payload, table_name, _job_config = saved_payloads[-1]
    assert table_name == "proj-y.dataset.feedback"
    assert payload[0]["feedback"] == "ótimo"

    module.save_cor_alert_in_bq(
        alert_id="a1",
        user_id="u2",
        alert_type="alagamento",
        severity="alta",
        description="Rua alagada",
        address="Rua A",
        latitude=-22.9,
        longitude=-43.2,
        timestamp="2026-04-08T13:00:00",
        environment="staging",
        dataset_id="dataset",
        table_id="cor_alerts",
        project_id="proj-z",
    )
    payload, table_name, _job_config = saved_payloads[-1]
    assert table_name == "proj-z.dataset.cor_alerts"
    assert payload[0]["alert_type"] == "alagamento"

    module.save_cor_alert_to_queue(
        alert_id="a2",
        user_id="u3",
        alert_type="enchente",
        severity="critica",
        description="Água alta",
        address="Rua B",
        latitude=None,
        longitude=None,
        timestamp="2026-04-08T14:00:00",
        environment="prod",
        bairro_raw="Jd America",
        bairro_normalizado="jardim america",
        dataset_id="dataset",
        table_id="queue",
        project_id="proj-q",
    )
    payload, table_name, _job_config = saved_payloads[-1]
    assert table_name == "proj-q.dataset.queue"
    assert payload[0]["status"] == "pending"
    assert payload[0]["bairro_normalizado"] == "jardim america"


@pytest.mark.asyncio
async def test_bigquery_background_helpers(monkeypatch):
    ensure_package("src", PROJECT_ROOT / "src")
    ensure_package("src.config", PROJECT_ROOT / "src" / "config")
    ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")

    env_module = types.SimpleNamespace(
        GCP_SERVICE_ACCOUNT_CREDENTIALS=base64.b64encode(
            json.dumps({"project_id": "proj-1"}).encode("utf-8")
        ).decode("utf-8"),
        GOOGLE_BIGQUERY_PAGE_SIZE=10,
        ENVIRONMENT="test",
    )
    monkeypatch.setitem(sys.modules, "src.config.env", env_module)
    monkeypatch.setitem(
        sys.modules, "src.config", types.SimpleNamespace(env=env_module)
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.error_interceptor",
        types.SimpleNamespace(interceptor=passthrough_interceptor),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.log",
        types.SimpleNamespace(
            logger=types.SimpleNamespace(
                info=lambda *_a, **_k: None,
                error=lambda *_a, **_k: None,
                warning=lambda *_a, **_k: None,
                exception=lambda *_a, **_k: None,
            )
        ),
    )

    module = load_module("test_bigquery_background_module", "src/utils/bigquery.py")
    bigquery_stub = types.SimpleNamespace(
        SchemaField=lambda *args, **kwargs: ("schema", args, kwargs),
        LoadJobConfig=lambda **kwargs: types.SimpleNamespace(**kwargs),
        TimePartitioning=lambda **kwargs: types.SimpleNamespace(**kwargs),
        TimePartitioningType=types.SimpleNamespace(DAY="DAY"),
        SchemaUpdateOption=types.SimpleNamespace(
            ALLOW_FIELD_ADDITION="ALLOW_FIELD_ADDITION"
        ),
    )
    monkeypatch.setattr(module, "bigquery", bigquery_stub)

    calls = []

    class FakeLoop:
        async def run_in_executor(self, executor, func, *args):
            calls.append((func, args))
            return func(*args)

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: FakeLoop())

    monkeypatch.setattr(
        module,
        "save_feedback_in_bq",
        lambda *args: {"kind": "feedback", "args": args},
    )
    monkeypatch.setattr(
        module,
        "save_cor_alert_to_queue",
        lambda *args: {"kind": "queue", "args": args},
    )
    monkeypatch.setattr(
        module,
        "save_response_in_bq",
        lambda *args: {"kind": "response", "args": args},
    )

    saved_payloads = []

    class FakeJob:
        def result(self):
            return None

    class FakeClient:
        def load_table_from_json(self, payload, table_name, job_config=None):
            saved_payloads.append((payload, table_name, job_config))
            return FakeJob()

    monkeypatch.setattr(module, "get_bigquery_client", lambda: FakeClient())

    await module.save_feedback_in_bq_background(
        user_id="u1",
        feedback="bom",
        timestamp="2026-04-08T12:00:00",
        environment="staging",
    )
    assert calls[0][0] is module.save_feedback_in_bq

    await module.save_response_in_bq_background(
        data={"ok": True},
        endpoint="endpoint",
        dataset_id="dataset",
        table_id="table",
        environment="test",
    )
    assert calls[1][0] is module.save_response_in_bq

    await module.save_cor_alert_in_bq_background(
        alert_id="a3",
        user_id="u4",
        alert_type="alagamento",
        severity="alta",
        description="descrição",
        address="Rua do Jd América, 10",
        latitude=-22.9,
        longitude=-43.2,
        timestamp="2026-04-08T15:00:00",
        environment="prod",
        bairro_raw="",
        bairro_normalizado="jd america",
        dataset_id="dataset",
        table_id="alerts",
    )
    payload, table_name, _job_config = saved_payloads[-1]
    assert table_name == "rj-iplanrio.dataset.alerts"
    assert payload[0]["bairro_normalizado"] == "jardim america"
    assert payload[0]["bairro_raw"] == "jardim america"

    await module.save_cor_alert_to_queue_background(
        alert_id="a4",
        user_id="u5",
        alert_type="enchente",
        severity="alta",
        description="descrição",
        address="Rua C",
        latitude=-22.9,
        longitude=-43.2,
        timestamp="2026-04-08T16:00:00",
        environment="staging",
    )
    assert calls[-1][0] is module.save_cor_alert_to_queue
