"""Testes do workaround de cadeia TLS do SGRC (src/utils/sgrc_ca.py)."""

import os

import certifi

from src.utils import sgrc_ca


def test_ensure_sgrc_ca_chain_sets_env_and_bundle(monkeypatch):
    """Seta SSL_CERT_FILE/REQUESTS_CA_BUNDLE pro bundle, que é superset do certifi
    e contém o intermediário GeoTrust (corrige a cadeia incompleta do SGRC)."""
    monkeypatch.setattr(sgrc_ca, "_applied", False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

    sgrc_ca.ensure_sgrc_ca_chain()

    assert os.environ["SSL_CERT_FILE"] == sgrc_ca._BUNDLE_PATH
    assert os.environ["REQUESTS_CA_BUNDLE"] == sgrc_ca._BUNDLE_PATH

    bundle = open(sgrc_ca._BUNDLE_PATH, encoding="utf-8").read()
    # intermediário presente
    assert sgrc_ca._GEOTRUST_TLS_RSA_CA_G1_PEM.strip() in bundle
    # superset do certifi: todos os roots padrão continuam (não quebra outro TLS)
    base = open(certifi.where(), encoding="utf-8").read()
    assert bundle.count("BEGIN CERTIFICATE") >= base.count("BEGIN CERTIFICATE")


def test_ensure_sgrc_ca_chain_is_idempotent(monkeypatch):
    """Segunda chamada é no-op (não re-escreve nem re-loga)."""
    monkeypatch.setattr(sgrc_ca, "_applied", False)
    sgrc_ca.ensure_sgrc_ca_chain()
    first = os.environ.get("SSL_CERT_FILE")
    sgrc_ca.ensure_sgrc_ca_chain()
    assert os.environ.get("SSL_CERT_FILE") == first
    assert sgrc_ca._applied is True


def test_intermediate_pem_is_valid_geotrust():
    """O PEM embutido é o GeoTrust TLS RSA CA G1 (sanity do conteúdo)."""
    pem = sgrc_ca._GEOTRUST_TLS_RSA_CA_G1_PEM
    assert pem.strip().startswith("-----BEGIN CERTIFICATE-----")
    assert pem.strip().endswith("-----END CERTIFICATE-----")


def test_failsafe_never_raises_and_does_not_set_env(monkeypatch):
    """Contrato crítico: se montar o bundle falhar, NÃO derruba o boot e NÃO seta
    env meia-boca (SGRC fica como antes, sem regressão)."""
    monkeypatch.setattr(sgrc_ca, "_applied", False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

    def _boom():
        raise RuntimeError("certifi indisponível")

    monkeypatch.setattr(sgrc_ca.certifi, "where", _boom)

    # não deve levantar
    sgrc_ca.ensure_sgrc_ca_chain()

    assert sgrc_ca._applied is False
    assert "SSL_CERT_FILE" not in os.environ
    assert "REQUESTS_CA_BUNDLE" not in os.environ


def test_dedup_when_certifi_already_has_intermediate(monkeypatch, tmp_path):
    """Se o certifi JÁ contiver o intermediário, o bundle não o duplica (aparece
    1×) e a env ainda é setada."""
    fake_certifi = tmp_path / "cacert.pem"
    # certifi "de mentira" que já inclui o intermediário GeoTrust
    fake_certifi.write_text(
        "# fake roots\n" + sgrc_ca._GEOTRUST_TLS_RSA_CA_G1_PEM, encoding="utf-8"
    )
    monkeypatch.setattr(sgrc_ca, "_applied", False)
    monkeypatch.setattr(sgrc_ca.certifi, "where", lambda: str(fake_certifi))

    sgrc_ca.ensure_sgrc_ca_chain()

    assert os.environ["SSL_CERT_FILE"] == sgrc_ca._BUNDLE_PATH
    bundle = open(sgrc_ca._BUNDLE_PATH, encoding="utf-8").read()
    # intermediário não duplicado
    marker = "MIIEjTCCA3WgAwIBAgIQDQd4KhM/xvmlcpbhMf/ReTANBgkqhkiG9w0BAQsF"
    assert bundle.count(marker) == 1
