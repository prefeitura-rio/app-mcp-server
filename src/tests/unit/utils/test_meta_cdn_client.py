"""
Tests pra src/utils/meta_cdn_client.py — Meta Graph API media download
(metadata + signed URL). httpx é mocked (sem rede real).
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
def meta_cdn_module(monkeypatch):
    """Carrega meta_cdn_client com env stub + loguru stub."""
    log_messages: list = []

    def _log(*args, **kwargs):
        log_messages.append(("info", args, kwargs))

    fake_logger = types.SimpleNamespace(
        info=_log,
        warning=_log,
        error=_log,
        debug=_log,
    )
    fake_loguru = types.ModuleType("loguru")
    fake_loguru.logger = fake_logger
    monkeypatch.setitem(sys.modules, "loguru", fake_loguru)

    fake_env_module = types.ModuleType("src.config.env")
    fake_env_module.WA_TOKEN = "EAAtest-token"
    fake_config_pkg = types.ModuleType("src.config")
    fake_config_pkg.env = fake_env_module
    monkeypatch.setitem(sys.modules, "src.config", fake_config_pkg)
    monkeypatch.setitem(sys.modules, "src.config.env", fake_env_module)

    _ensure_package("src", PROJECT_ROOT / "src")
    _ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")

    spec = importlib.util.spec_from_file_location(
        "src.utils.meta_cdn_client",
        PROJECT_ROOT / "src" / "utils" / "meta_cdn_client.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, log_messages


class _FakeResp:
    def __init__(self, status_code: int, json_data=None, content: bytes = b""):
        self.status_code = status_code
        self._json = json_data or {}
        self.content = content
        self.text = ""

    def json(self):
        return self._json


class _FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        self.timeout = kwargs.get("timeout")
        self._responses = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, headers=None, follow_redirects=False):
        # Lookup by URL prefix
        for prefix, resp in _FakeAsyncClient._registry.items():
            if url.startswith(prefix):
                return resp
        return _FakeResp(404)

    _registry: dict = {}


@pytest.mark.asyncio
async def test_fetch_media_url_ok(meta_cdn_module, monkeypatch):
    module, _logs = meta_cdn_module
    _FakeAsyncClient._registry = {
        "https://graph.facebook.com/v23.0/1234567890123456": _FakeResp(
            200,
            json_data={
                "url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1234567890123456",
                "mime_type": "image/jpeg",
                "sha256": "abc123",
                "file_size": 50000,
                "messaging_product": "whatsapp",
                "id": "1234567890123456",
            },
        ),
    }
    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeAsyncClient)

    url, mime, size = await module.fetch_media_url("1234567890123456")
    assert url.startswith("https://lookaside.fbsbx.com/")
    assert mime == "image/jpeg"
    assert size == 50000


@pytest.mark.asyncio
async def test_fetch_media_url_missing_token(meta_cdn_module, monkeypatch):
    module, _ = meta_cdn_module
    # Sobrescreve token pra vazio
    monkeypatch.setattr(module.env, "WA_TOKEN", None)
    with pytest.raises(module.MetaCDNError, match="WA_TOKEN"):
        await module.fetch_media_url("1234567890123456")


@pytest.mark.asyncio
async def test_fetch_media_url_empty_id(meta_cdn_module):
    module, _ = meta_cdn_module
    with pytest.raises(module.MetaCDNError, match="meta_media_id vazio"):
        await module.fetch_media_url("")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_id",
    [
        "../../me",  # path traversal
        "123?fields=phone_numbers",  # query injection
        "123/files",  # extra path segment
        "abc",  # non-numeric
        "12345abc",  # mix
        " 123",  # whitespace
        "123" * 20,  # too long (60 chars)
    ],
)
async def test_fetch_media_url_rejects_malformed_id(meta_cdn_module, bad_id):
    module, _ = meta_cdn_module
    with pytest.raises(module.MetaCDNError, match="formato inválido"):
        await module.fetch_media_url(bad_id)


@pytest.mark.asyncio
async def test_fetch_media_url_4xx_raises(meta_cdn_module, monkeypatch):
    module, _ = meta_cdn_module
    _FakeAsyncClient._registry = {
        "https://graph.facebook.com/v23.0/9999999999": _FakeResp(403, json_data={}),
    }
    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeAsyncClient)
    with pytest.raises(module.MetaCDNError, match="403"):
        await module.fetch_media_url("9999999999")


@pytest.mark.asyncio
async def test_fetch_media_url_no_url_key(meta_cdn_module, monkeypatch):
    module, _ = meta_cdn_module
    _FakeAsyncClient._registry = {
        "https://graph.facebook.com/v23.0/8888888888": _FakeResp(
            200,
            json_data={"mime_type": "image/jpeg"},  # missing url
        ),
    }
    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeAsyncClient)
    with pytest.raises(module.MetaCDNError, match="sem 'url'"):
        await module.fetch_media_url("8888888888")


@pytest.mark.asyncio
async def test_fetch_media_url_oversize_rejects(meta_cdn_module, monkeypatch):
    module, _ = meta_cdn_module
    huge = module._MAX_BYTES + 1
    _FakeAsyncClient._registry = {
        "https://graph.facebook.com/v23.0/7777777777": _FakeResp(
            200,
            json_data={
                "url": "https://lookaside.fbsbx.com/x",
                "mime_type": "video/mp4",
                "file_size": huge,
            },
        ),
    }
    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeAsyncClient)
    with pytest.raises(module.MetaCDNError, match="excede cap"):
        await module.fetch_media_url("7777777777")


@pytest.mark.asyncio
async def test_download_signed_url_ok(meta_cdn_module, monkeypatch):
    module, _ = meta_cdn_module
    _FakeAsyncClient._registry = {
        "https://lookaside.fbsbx.com/x": _FakeResp(
            200, content=b"\xff\xd8\xff" + b"X" * 1000
        ),
    }
    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeAsyncClient)
    blob = await module.download_signed_url("https://lookaside.fbsbx.com/x")
    assert blob.startswith(b"\xff\xd8\xff")  # JPEG magic
    assert len(blob) == 1003


@pytest.mark.asyncio
async def test_download_signed_url_4xx_raises(meta_cdn_module, monkeypatch):
    module, _ = meta_cdn_module
    _FakeAsyncClient._registry = {
        "https://lookaside.fbsbx.com/expired": _FakeResp(401, content=b""),
    }
    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeAsyncClient)
    with pytest.raises(module.MetaCDNError, match="401"):
        await module.download_signed_url("https://lookaside.fbsbx.com/expired")


@pytest.mark.asyncio
async def test_download_signed_url_oversize_rejects(meta_cdn_module, monkeypatch):
    module, _ = meta_cdn_module
    huge_blob = b"\xff\xd8\xff" + b"X" * (module._MAX_BYTES + 100)
    _FakeAsyncClient._registry = {
        "https://lookaside.fbsbx.com/big": _FakeResp(200, content=huge_blob),
    }
    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeAsyncClient)
    with pytest.raises(module.MetaCDNError, match="excedem cap"):
        await module.download_signed_url("https://lookaside.fbsbx.com/big")


@pytest.mark.asyncio
async def test_download_meta_media_wrapper(meta_cdn_module, monkeypatch):
    module, _ = meta_cdn_module
    _FakeAsyncClient._registry = {
        "https://graph.facebook.com/v23.0/5555555555": _FakeResp(
            200,
            json_data={
                "url": "https://lookaside.fbsbx.com/wrap",
                "mime_type": "audio/ogg",
                "file_size": 14336,
            },
        ),
        "https://lookaside.fbsbx.com/wrap": _FakeResp(
            200, content=b"OggS" + b"AUDIO" * 100
        ),
    }
    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeAsyncClient)
    blob, mime = await module.download_meta_media("5555555555")
    assert blob.startswith(b"OggS")
    assert mime == "audio/ogg"
