"""
Tests pra src/utils/salesforce_client.py — OAuth Client Credentials + download
de ContentVersion via SF REST. httpx é mocked (sem rede real).
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[4]


def _ensure_package(name: str, path: Path) -> types.ModuleType:
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(path)]
    sys.modules[name] = pkg
    return pkg


@pytest.fixture
def sf_client_module(monkeypatch):
    """Carrega salesforce_client com env stub + log stub. Reset cache por test."""
    log_messages: list = []
    fake_logger = types.SimpleNamespace(
        info=lambda msg: log_messages.append(("info", msg)),
        warning=lambda msg: log_messages.append(("warning", msg)),
        error=lambda msg: log_messages.append(("error", msg)),
        debug=lambda msg: log_messages.append(("debug", msg)),
    )
    fake_log_module = types.ModuleType("src.utils.log")
    fake_log_module.logger = fake_logger
    monkeypatch.setitem(sys.modules, "src.utils.log", fake_log_module)

    fake_env = types.SimpleNamespace(
        SALESFORCE_INSTANCE_URL="https://example.my.salesforce.com",
        SALESFORCE_CLIENT_ID="test-client-id",
        SALESFORCE_CLIENT_SECRET="test-client-secret",
    )
    fake_env_module = types.ModuleType("src.config.env")
    for k in (
        "SALESFORCE_INSTANCE_URL",
        "SALESFORCE_CLIENT_ID",
        "SALESFORCE_CLIENT_SECRET",
    ):
        setattr(fake_env_module, k, getattr(fake_env, k))
    fake_config_pkg = types.ModuleType("src.config")
    fake_config_pkg.env = fake_env_module
    monkeypatch.setitem(sys.modules, "src.config", fake_config_pkg)
    monkeypatch.setitem(sys.modules, "src.config.env", fake_env_module)

    _ensure_package("src", PROJECT_ROOT / "src")
    _ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")

    spec = importlib.util.spec_from_file_location(
        "src.utils.salesforce_client",
        PROJECT_ROOT / "src" / "utils" / "salesforce_client.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["src.utils.salesforce_client"] = module
    spec.loader.exec_module(module)

    # Reset token cache pra cada test (módulo é singleton)
    module._cached_token = None
    module._cached_token_at = 0.0
    return module, log_messages


class FakeResponse:
    def __init__(self, status_code, body=None, json_data=None):
        self.status_code = status_code
        self._body = body or b""
        self._json = json_data
        self.text = (
            body.decode("utf-8", errors="replace")
            if isinstance(body, bytes)
            else str(body or "")
        )

    @property
    def content(self):
        return self._body

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json


class FakeStreamResponse:
    """Resposta com .stream-like (header + iter_bytes), pra _stream_with_limit."""

    def __init__(self, status_code, body=b"", content_length=None):
        self.status_code = status_code
        self._body = body
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_bytes(self, chunk_size=64 * 1024):
        # Yields the body in 1 chunk (simplest). Tests podem precisar de
        # múltiplos chunks pra testar streaming abort — adicionar
        # `chunks=[...]` param se necessário.
        if self._body:
            yield self._body


class FakeClient:
    """Substitui httpx.Client com sequência programada de responses.

    Suporta:
      - POST com FakeResponse (OAuth)
      - stream("GET", ...) com FakeStreamResponse (download)
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._responses.pop(0)

    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self._responses.pop(0)


# ---------- _is_safe_download_path ----------


def test_safe_path_accepts_content_version_version_data_15char(sf_client_module):
    sfc, _ = sf_client_module
    assert sfc._is_safe_download_path(
        "/services/data/v62.0/sobjects/ContentVersion/0688800000Bgd3T/VersionData"
    )


def test_safe_path_accepts_content_version_version_data_18char(sf_client_module):
    sfc, _ = sf_client_module
    assert sfc._is_safe_download_path(
        "/services/data/v62.0/sobjects/ContentVersion/0688800000Bgd3TAAR/VersionData"
    )


def test_safe_path_rejects_absolute_url(sf_client_module):
    sfc, _ = sf_client_module
    assert not sfc._is_safe_download_path("https://evil.example/file")
    assert not sfc._is_safe_download_path("http://evil.example/file")
    assert not sfc._is_safe_download_path("//evil.example/file")


def test_safe_path_rejects_path_traversal(sf_client_module):
    sfc, _ = sf_client_module
    assert not sfc._is_safe_download_path("/services/data/../etc/passwd")


def test_safe_path_rejects_soql_query_endpoint(sf_client_module):
    """SOQL query endpoint daria acesso a dados privilegiados — must reject."""
    sfc, _ = sf_client_module
    assert not sfc._is_safe_download_path(
        "/services/data/v62.0/query/?q=SELECT+Id+FROM+User"
    )
    assert not sfc._is_safe_download_path("/services/data/v62.0/query/")


def test_safe_path_rejects_other_sobjects(sf_client_module):
    """Outros sObjects (Account, User, Case) não devem passar."""
    sfc, _ = sf_client_module
    assert not sfc._is_safe_download_path("/services/data/v62.0/sobjects/User/005xxx")
    assert not sfc._is_safe_download_path(
        "/services/data/v62.0/sobjects/Account/001xxx"
    )
    assert not sfc._is_safe_download_path("/services/data/v62.0/sobjects/Case/500xxx")


def test_safe_path_rejects_content_version_describe_or_metadata(sf_client_module):
    """Outras rotas em ContentVersion (describe, list) não são VersionData."""
    sfc, _ = sf_client_module
    assert not sfc._is_safe_download_path(
        "/services/data/v62.0/sobjects/ContentVersion/0688800000Bgd3T"
    )
    assert not sfc._is_safe_download_path(
        "/services/data/v62.0/sobjects/ContentVersion/describe"
    )


def test_safe_path_rejects_query_string_suffix(sf_client_module):
    """Query string anexada quebra o pattern exato — must reject."""
    sfc, _ = sf_client_module
    assert not sfc._is_safe_download_path(
        "/services/data/v62.0/sobjects/ContentVersion/0688800000Bgd3T/VersionData?foo=bar"
    )


def test_safe_path_rejects_arbitrary_endpoint(sf_client_module):
    sfc, _ = sf_client_module
    assert not sfc._is_safe_download_path("/secur/frontdoor.jsp")
    assert not sfc._is_safe_download_path("/admin")


def test_safe_path_rejects_non_string(sf_client_module):
    sfc, _ = sf_client_module
    assert not sfc._is_safe_download_path(None)
    assert not sfc._is_safe_download_path(123)


# ---------- _config_ready ----------


def test_config_ready_true_when_all_set(sf_client_module):
    sfc, _ = sf_client_module
    assert sfc._config_ready()


def test_config_ready_false_when_missing(sf_client_module, monkeypatch):
    sfc, _ = sf_client_module
    import sys

    env_mod = sys.modules["src.config.env"]
    monkeypatch.setattr(env_mod, "SALESFORCE_CLIENT_SECRET", None)
    assert not sfc._config_ready()


# ---------- download_content_version: happy path ----------


def test_download_happy_path(sf_client_module, monkeypatch):
    sfc, _ = sf_client_module
    # Sequência: 1 POST /oauth (200 com token) + 1 stream GET (200 com bytes)
    fake = FakeClient(
        [
            FakeResponse(200, json_data={"access_token": "abc.token.123"}),
            FakeStreamResponse(200, body=b"\xff\xd8FAKE_JPEG_BYTES"),
        ]
    )
    monkeypatch.setattr(sfc.httpx, "Client", lambda **kw: fake)

    result = sfc.download_content_version(
        "/services/data/v62.0/sobjects/ContentVersion/0688800000Bgd3T/VersionData"
    )
    assert result == b"\xff\xd8FAKE_JPEG_BYTES"
    assert len(fake.calls) == 2
    assert fake.calls[0][0] == "POST"
    assert "/services/oauth2/token" in fake.calls[0][1]
    assert fake.calls[1][0] == "GET"
    assert fake.calls[1][1].endswith(
        "/services/data/v62.0/sobjects/ContentVersion/0688800000Bgd3T/VersionData"
    )
    assert fake.calls[1][2]["headers"]["Authorization"] == "Bearer abc.token.123"


# ---------- 401 refresh + retry ----------


def test_download_refreshes_token_on_401_and_retries(sf_client_module, monkeypatch):
    sfc, _ = sf_client_module
    # Token1 → 401 → Token2 → 200 (via stream)
    fake = FakeClient(
        [
            FakeResponse(200, json_data={"access_token": "t1"}),  # OAuth #1
            FakeStreamResponse(401, body=b""),  # stream GET retorna 401
            FakeResponse(200, json_data={"access_token": "t2"}),  # OAuth #2 refresh
            FakeStreamResponse(200, body=b"BYTES_OK"),  # stream GET retry sucesso
        ]
    )
    monkeypatch.setattr(sfc.httpx, "Client", lambda **kw: fake)

    result = sfc.download_content_version(
        "/services/data/v62.0/sobjects/ContentVersion/0688800000Bgd3T/VersionData"
    )
    assert result == b"BYTES_OK"
    assert len(fake.calls) == 4
    # Retry stream usa token novo
    assert fake.calls[3][2]["headers"]["Authorization"] == "Bearer t2"


# ---------- failure modes ----------


def test_download_returns_none_on_oauth_failure(sf_client_module, monkeypatch):
    sfc, _ = sf_client_module
    fake = FakeClient([FakeResponse(400, body=b"bad creds")])
    monkeypatch.setattr(sfc.httpx, "Client", lambda **kw: fake)

    assert (
        sfc.download_content_version(
            "/services/data/v62.0/sobjects/ContentVersion/0688800000Bgd3T/VersionData"
        )
        is None
    )


def test_download_returns_none_on_invalid_path(sf_client_module, monkeypatch, caplog):
    sfc, _ = sf_client_module
    # Sem mock — path inválido deve curto-circuitar antes de qualquer HTTP
    fake = FakeClient([])  # vazio: vai estourar se chamado
    monkeypatch.setattr(sfc.httpx, "Client", lambda **kw: fake)

    assert sfc.download_content_version("/secur/frontdoor.jsp") is None
    # Nenhuma chamada HTTP feita
    assert fake.calls == []


def test_download_returns_none_when_config_missing(sf_client_module, monkeypatch):
    sfc, _ = sf_client_module
    import sys

    env_mod = sys.modules["src.config.env"]
    monkeypatch.setattr(env_mod, "SALESFORCE_INSTANCE_URL", None)
    fake = FakeClient([])
    monkeypatch.setattr(sfc.httpx, "Client", lambda **kw: fake)

    assert (
        sfc.download_content_version(
            "/services/data/v62.0/sobjects/ContentVersion/0688800000Bgd3T/VersionData"
        )
        is None
    )
    assert fake.calls == []


def test_download_rejects_oversize_via_content_length(sf_client_module, monkeypatch):
    """Content-Length conhecido > limite: aborta antes de bufferizar bytes."""
    sfc, _ = sf_client_module
    monkeypatch.setattr(sfc, "_MAX_BYTES", 100)
    fake = FakeClient(
        [
            FakeResponse(200, json_data={"access_token": "t"}),
            FakeStreamResponse(200, body=b"X" * 500, content_length=500),
        ]
    )
    monkeypatch.setattr(sfc.httpx, "Client", lambda **kw: fake)

    assert (
        sfc.download_content_version(
            "/services/data/v62.0/sobjects/ContentVersion/0688800000Bgd3T/VersionData"
        )
        is None
    )


def test_download_rejects_oversize_via_stream_check(sf_client_module, monkeypatch):
    """Sem Content-Length: aborta enquanto streama chunks (cobertura defesa)."""
    sfc, _ = sf_client_module
    monkeypatch.setattr(sfc, "_MAX_BYTES", 100)
    fake = FakeClient(
        [
            FakeResponse(200, json_data={"access_token": "t"}),
            FakeStreamResponse(200, body=b"X" * 500),  # sem Content-Length header
        ]
    )
    monkeypatch.setattr(sfc.httpx, "Client", lambda **kw: fake)

    assert (
        sfc.download_content_version(
            "/services/data/v62.0/sobjects/ContentVersion/0688800000Bgd3T/VersionData"
        )
        is None
    )


# ---------- token cache ----------


def test_token_cached_across_calls(sf_client_module, monkeypatch):
    sfc, _ = sf_client_module
    # 1 OAuth + 2 streams sucesso (segunda call reusa token cached, sem novo OAuth)
    fake = FakeClient(
        [
            FakeResponse(200, json_data={"access_token": "cached-t"}),
            FakeStreamResponse(200, body=b"FIRST"),
            FakeStreamResponse(200, body=b"SECOND"),
        ]
    )
    monkeypatch.setattr(sfc.httpx, "Client", lambda **kw: fake)

    first = sfc.download_content_version(
        "/services/data/v62.0/sobjects/ContentVersion/0688800000Bgd3A/VersionData"
    )
    second = sfc.download_content_version(
        "/services/data/v62.0/sobjects/ContentVersion/0688800000Bgd3B/VersionData"
    )
    assert first == b"FIRST"
    assert second == b"SECOND"
    # POST OAuth apenas 1x; 2 streams GET
    posts = [c for c in fake.calls if c[0] == "POST"]
    gets = [c for c in fake.calls if c[0] == "GET"]
    assert len(posts) == 1
    assert len(gets) == 2


def test_async_wrapper_offloads_to_thread(sf_client_module, monkeypatch):
    """download_content_version_async deve cumprir contrato awaitable e
    devolver mesmo resultado do sync."""
    import asyncio

    sfc, _ = sf_client_module
    fake = FakeClient(
        [
            FakeResponse(200, json_data={"access_token": "t"}),
            FakeStreamResponse(200, body=b"ASYNC_OK"),
        ]
    )
    monkeypatch.setattr(sfc.httpx, "Client", lambda **kw: fake)

    result = asyncio.run(
        sfc.download_content_version_async(
            "/services/data/v62.0/sobjects/ContentVersion/0688800000Bgd3X/VersionData"
        )
    )
    assert result == b"ASYNC_OK"
