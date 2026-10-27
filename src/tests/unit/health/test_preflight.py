"""Testes do preflight de configuração (src/health/preflight.py)."""

import base64
import json

import pytest

from src.health import preflight


@pytest.fixture(autouse=True)
def _reset_gcp_cache():
    preflight.reset_gcp_credentials_cache()
    yield
    preflight.reset_gcp_credentials_cache()


@pytest.fixture
def full_env(monkeypatch, tmp_path):
    """Ambiente completo e válido, para os testes partirem de um estado limpo."""
    for name in preflight.REQUIRED_ENV_VARS:
        monkeypatch.setenv(name, f"valor-{name.lower()}")

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for filename in preflight.REQUIRED_DATA_FILES:
        (data_dir / filename).write_text("[]")

    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("VALID_TOKENS", "token-a,token-b")
    monkeypatch.setenv("REDIS_URL", "redis://:senha-secreta@localhost:6379/0")
    monkeypatch.setenv(
        "GCP_SERVICE_ACCOUNT_CREDENTIALS",
        base64.b64encode(json.dumps({"type": "service_account"}).encode()).decode(),
    )
    return data_dir


@pytest.fixture(autouse=True)
def _stub_service_account(monkeypatch):
    """Evita gerar uma chave RSA real: só o caminho de parse nos interessa."""
    from google.oauth2 import service_account

    monkeypatch.setattr(
        service_account.Credentials,
        "from_service_account_info",
        classmethod(lambda cls, info, **kwargs: object()),
    )


def test_ambiente_completo_nao_produz_erros(full_env):
    assert preflight.collect_preflight_errors() == []


def test_reporta_todas_as_variaveis_faltantes_de_uma_vez(full_env, monkeypatch):
    monkeypatch.delenv("SGRC_URL")
    monkeypatch.delenv("IPTU_API_URL")
    monkeypatch.delenv("PROXY_URL")

    errors = preflight.check_required_env()

    # O ponto do preflight: as três aparecem juntas, não uma por deploy.
    assert len(errors) == 3
    joined = " ".join(errors)
    for name in ("SGRC_URL", "IPTU_API_URL", "PROXY_URL"):
        assert name in joined


def test_variavel_vazia_conta_como_ausente(full_env, monkeypatch):
    monkeypatch.setenv("SGRC_URL", "   ")
    assert any("SGRC_URL" in error for error in preflight.check_required_env())


@pytest.mark.parametrize(
    ("valor", "trecho_esperado"),
    [
        ("nao-e-base64!!", "base64"),
        (base64.b64encode(b"nao-e-json").decode(), "JSON"),
        (base64.b64encode(b'"uma-string"').decode(), "objeto JSON"),
    ],
)
def test_credencial_gcp_invalida_e_detectada(
    full_env, monkeypatch, valor, trecho_esperado
):
    monkeypatch.setenv("GCP_SERVICE_ACCOUNT_CREDENTIALS", valor)
    error = preflight.check_gcp_credentials()
    assert error is not None
    assert trecho_esperado in error


def test_credencial_gcp_e_memoizada_por_valor(full_env, monkeypatch):
    chamadas = []

    monkeypatch.setattr(
        preflight,
        "_validate_gcp_credentials",
        lambda raw: chamadas.append(raw) or None,
    )

    preflight.check_gcp_credentials()
    preflight.check_gcp_credentials()
    assert len(chamadas) == 1, "mesmo valor não deve revalidar"

    monkeypatch.setenv("GCP_SERVICE_ACCOUNT_CREDENTIALS", "outro-valor")
    preflight.check_gcp_credentials()
    assert len(chamadas) == 2, "valor diferente deve revalidar"


@pytest.mark.parametrize("valor", ["token-a,,token-b", "token-a,", ",token-a"])
def test_valid_tokens_com_entrada_vazia_e_rejeitado(full_env, monkeypatch, valor):
    monkeypatch.setenv("VALID_TOKENS", valor)
    assert preflight.check_valid_tokens() is not None


def test_valid_tokens_bem_formado_passa(full_env, monkeypatch):
    monkeypatch.setenv("VALID_TOKENS", "token-a, token-b")
    assert preflight.check_valid_tokens() is None


def test_arquivo_de_dados_ausente_e_detectado(full_env, monkeypatch):
    (full_env / "logradouros.json").unlink()
    errors = preflight.check_data_files()
    assert len(errors) == 1
    assert "logradouros.json" in errors[0]


def test_data_dir_inexistente_e_detectado(full_env, monkeypatch):
    monkeypatch.setenv("DATA_DIR", "/caminho/que/nao/existe")
    errors = preflight.check_data_files()
    assert len(errors) == 1
    assert "DATA_DIR" in errors[0]


def test_redis_url_malformada_nao_vaza_a_senha(full_env, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "protocolo-invalido://:senha-secreta@host:6379")
    error = preflight.check_redis_url()
    assert error is not None
    assert "senha-secreta" not in error


def test_preflight_aborta_o_processo_quando_ha_erro(full_env, monkeypatch):
    monkeypatch.delenv("REDIS_URL")

    with pytest.raises(SystemExit) as exc_info:
        preflight.run_startup_preflight()

    assert exc_info.value.code == 1


def test_preflight_nao_aborta_com_ambiente_valido(full_env):
    preflight.run_startup_preflight()  # não deve levantar


def test_lista_de_modelos_da_busca_ausente_e_valida(full_env, monkeypatch):
    monkeypatch.delenv("GEMINI_SEARCH_MODELS", raising=False)
    assert preflight.check_gemini_search_config() == []


def test_lista_de_modelos_da_busca_valida_passa(full_env, monkeypatch):
    monkeypatch.setenv(
        "GEMINI_SEARCH_MODELS", '[{"model": "gemini-3.5-flash-lite", "timeout_s": 20}]'
    )
    assert preflight.check_gemini_search_config() == []


def test_lista_de_modelos_da_busca_com_limite_de_consultas_passa(full_env, monkeypatch):
    """O valor de produção depois do CHATR-314."""
    monkeypatch.setenv(
        "GEMINI_SEARCH_MODELS",
        '[{"model": "gemini-2.5-flash-lite", "timeout_s": 35, "thinking_budget": 1024}, '
        '{"model": "gemini-3.5-flash-lite", "timeout_s": 20, '
        '"thinking_level": "MINIMAL", "max_search_queries": 1}]',
    )
    assert preflight.check_gemini_search_config() == []


def test_limite_de_consultas_invalido_aborta_o_boot(full_env, monkeypatch):
    monkeypatch.setenv(
        "GEMINI_SEARCH_MODELS",
        '[{"model": "gemini-3.5-flash-lite", "max_search_queries": 0}]',
    )

    assert preflight.check_gemini_search_config() == [
        "GEMINI_SEARCH_MODELS[0] (gemini-3.5-flash-lite).max_search_queries: "
        "precisa ser ≥ 1 (recebido: 0)"
    ]


def test_lista_de_modelos_da_busca_invalida_aborta_o_boot(full_env, monkeypatch):
    """Cada problema da lista vira um erro, junto com os demais do preflight."""
    monkeypatch.setenv(
        "GEMINI_SEARCH_MODELS",
        '[{"model": "gemini-2.5-flash-lite", "timout_s": 45}, {"timeout_s": 20}]',
    )

    errors = preflight.collect_preflight_errors()

    assert errors == [
        "GEMINI_SEARCH_MODELS[0] (gemini-2.5-flash-lite).timout_s: campo desconhecido "
        "(recebido: 45)",
        "GEMINI_SEARCH_MODELS[1].model: campo obrigatório ausente",
    ]
    with pytest.raises(SystemExit):
        preflight.run_startup_preflight()
