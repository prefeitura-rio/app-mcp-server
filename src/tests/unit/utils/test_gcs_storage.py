"""
Testes do upload compartilhado no bucket de workflows.

O que varia entre os chamadores — caminho do blob, content-type e validade da
signed URL — entra por parâmetro, então é isso que os testes fixam: o helper
repassa cada um sem reinterpretar.
"""

import datetime as dt
import types

import pytest

import src.utils.gcs_storage as gcs_mod
from src.utils.gcs_storage import upload_to_gcs


class FakeBlob:
    def __init__(self):
        self.content = None
        self.content_type = None
        self.expiration = None

    def upload_from_string(self, content, content_type):
        self.content = content
        self.content_type = content_type

    def generate_signed_url(self, expiration):
        self.expiration = expiration
        return "https://storage.example/signed"


class FakeBucket:
    def __init__(self):
        self.blob_value = FakeBlob()
        self.blob_name = None

    def blob(self, name):
        self.blob_name = name
        return self.blob_value


@pytest.fixture
def bucket(monkeypatch):
    fake_bucket = FakeBucket()
    monkeypatch.setattr(gcs_mod, "get_workflows_bucket", lambda: fake_bucket)
    return fake_bucket


@pytest.mark.asyncio
async def test_upload_de_texto_repassa_parametros(bucket):
    signed_url = await upload_to_gcs(
        conteudo="<html></html>",
        blob_path="iptu/qrcode-pix/abc.html",
        content_type="text/html; charset=utf-8",
        ttl=dt.timedelta(hours=24),
    )

    assert signed_url == "https://storage.example/signed"
    assert bucket.blob_name == "iptu/qrcode-pix/abc.html"
    assert bucket.blob_value.content == "<html></html>"
    assert bucket.blob_value.content_type == "text/html; charset=utf-8"
    assert bucket.blob_value.expiration == dt.timedelta(hours=24)


@pytest.mark.asyncio
async def test_upload_de_bytes_sobe_sem_reencodar(bucket):
    await upload_to_gcs(
        conteudo=b"%PDF-1.4",
        blob_path="iptu/abc.pdf",
        content_type="application/pdf",
        ttl=dt.timedelta(days=7),
    )

    assert bucket.blob_value.content == b"%PDF-1.4"
    assert bucket.blob_value.content_type == "application/pdf"
    assert bucket.blob_value.expiration == dt.timedelta(days=7)


def test_credenciais_vem_do_env_em_base64(monkeypatch):
    capturado = {}

    monkeypatch.setattr(
        gcs_mod,
        "env",
        types.SimpleNamespace(WORKFLOWS_GCP_SERVICE_ACCOUNT="eyJhIjogMX0="),
    )
    monkeypatch.setattr(
        gcs_mod.service_account.Credentials,
        "from_service_account_info",
        lambda info: capturado.setdefault("info", info),
    )

    gcs_mod.get_credentials_from_env()

    assert capturado["info"] == {"a": 1}


def test_bucket_usa_credenciais_e_nome_do_env(monkeypatch):
    capturado = {}

    monkeypatch.setattr(
        gcs_mod, "env", types.SimpleNamespace(WORKFLOWS_GCS_BUCKET="bucket-name")
    )
    monkeypatch.setattr(gcs_mod, "get_credentials_from_env", lambda: "credentials")

    class FakeClient:
        def __init__(self, credentials):
            capturado["credentials"] = credentials

        def bucket(self, name):
            capturado["bucket"] = name
            return "o-bucket"

    monkeypatch.setattr(gcs_mod, "storage", types.SimpleNamespace(Client=FakeClient))

    assert gcs_mod.get_workflows_bucket() == "o-bucket"
    assert capturado["credentials"] == "credentials"
    assert capturado["bucket"] == "bucket-name"
