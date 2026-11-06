"""
Runner E2E do preview de staging (DEV-415).

O runner é o gate do `deploy-staging.yaml`: se reprovar, o preview é abortado e
o Maestro reverte o merge. Dois comportamentos não podem regredir sem alguém
perceber:

- uma oscilação da PGM ("sem registros", indisponível) no happy path de
  `/emitir_guia` não reprova o deploy, mas uma regressão do nosso código, sim;
- o log público do Actions nunca recebe `api_descricao_erro` nem valores da
  resposta (número da execução fiscal, identificadores do contribuinte).

Como em `test_pgm_contract_runner.py`, o arquivo mora em `src/tests/e2e/` com
prefixo `run_` para não ser coletado; importá-lo por caminho permite testá-lo.
Os dados abaixo são sintéticos.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[4]

# Identificadores sintéticos: o teste confere que nenhum deles chega ao log.
EXECUCAO_FISCAL_FALSA = "0000000-00.0000.8.19.0001"
CDA_FALSA = "99999999999"
PAYLOAD_AVISTA = {"origem_solicitação": 0, "execucoes": [EXECUCAO_FISCAL_FALSA]}

SEM_REGISTROS = {
    "api_resposta_sucesso": False,
    "api_descricao_erro": (
        "Não foram encontrados registros para a execução fiscal "
        f"{EXECUCAO_FISCAL_FALSA}"
    ),
}
INDISPONIVEL = {
    "api_resposta_sucesso": False,
    "api_descricao_erro": (
        "O sistema de dívida ativa está temporariamente indisponível. "
        "Por favor, tente novamente em alguns instantes."
    ),
}
ERRO_GENERICO = {
    "api_resposta_sucesso": False,
    "api_descricao_erro": "Erro ao emitir guia à vista. Tente novamente mais tarde.",
}
ERRO_DE_NEGOCIO = {
    "api_resposta_sucesso": False,
    "api_descricao_erro": f"Execução fiscal {EXECUCAO_FISCAL_FALSA} já quitada",
}
SUCESSO = {
    "api_resposta_sucesso": True,
    "execucoes": [EXECUCAO_FISCAL_FALSA],
    "cdas": [CDA_FALSA],
    "codigo_de_barras": "81650000000-0 00000000000-0 00000000000-0 00000000000-0",
    "link": "https://example.invalid/guia.pdf",
}


@pytest.fixture
def runner(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "run_preview_e2e",
        PROJECT_ROOT / "src/tests/e2e/run_preview_e2e.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_preview_e2e"] = module
    spec.loader.exec_module(module)

    monkeypatch.setattr(module, "AVISTA_PAYLOAD", json.dumps(PAYLOAD_AVISTA))
    monkeypatch.setattr(module, "REGULARIZACAO_PAYLOAD", "")
    monkeypatch.setattr(module, "VALID_TOKENS", "token-sintetico")
    monkeypatch.setattr(module, "EXTERNAL_API_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(module, "EXTERNAL_API_RETRY_DELAY_SECONDS", 7)
    yield module
    sys.modules.pop("run_preview_e2e", None)


@pytest.fixture
def sleeps(runner, monkeypatch):
    chamadas = []
    monkeypatch.setattr(runner.time, "sleep", chamadas.append)
    return chamadas


def responder(runner, monkeypatch, respostas):
    """Faz `request_json` devolver as respostas em ordem e registra as rotas."""
    rotas = []
    fila = list(respostas)

    def fake_request_json(path, payload=None, token=None, timeout=None):
        rotas.append(path)
        status, corpo = fila.pop(0)
        raw = json.dumps(corpo)
        return status, raw, corpo

    monkeypatch.setattr(runner, "request_json", fake_request_json)
    return rotas


def assert_sem_dados_da_resposta(saida: str) -> None:
    assert EXECUCAO_FISCAL_FALSA not in saida
    assert CDA_FALSA not in saida
    assert "Não foram encontrados" not in saida
    assert "já quitada" not in saida
    assert "example.invalid" not in saida


def test_sem_registros_uma_vez_e_depois_sucesso_passa(
    runner, monkeypatch, sleeps, capsys
):
    rotas = responder(runner, monkeypatch, [(200, SEM_REGISTROS), (200, SUCESSO)])

    runner.run_emitir_guia_happy_paths()

    assert rotas == ["/emitir_guia", "/emitir_guia"]
    assert sleeps == [7]
    saida = capsys.readouterr().out
    assert "attempt 1/3" in saida
    assert "sem_registros" in saida
    assert "SKIPPED" not in saida
    assert_sem_dados_da_resposta(saida)


def test_sucesso_nunca_e_repetido(runner, monkeypatch, sleeps):
    """Cada emissão bem-sucedida gera uma guia real: uma chamada só."""
    rotas = responder(runner, monkeypatch, [(200, SUCESSO)])

    runner.run_emitir_guia_happy_paths()

    assert rotas == ["/emitir_guia"]
    assert sleeps == []


def test_sem_registros_persistente_vira_skip_explicito(
    runner, monkeypatch, sleeps, capsys
):
    rotas = responder(runner, monkeypatch, [(200, SEM_REGISTROS)] * 3)

    runner.run_emitir_guia_happy_paths()

    assert rotas == ["/emitir_guia"] * 3
    assert sleeps == [7, 7]
    saida = capsys.readouterr().out
    assert "emitir_guia happy path: SKIPPED - PGM answered 'sem_registros'" in saida
    assert "in 3 attempt(s)" in saida
    assert "FAIL" not in saida
    assert_sem_dados_da_resposta(saida)


def test_indisponivel_vira_skip_sem_nova_tentativa(runner, monkeypatch, sleeps, capsys):
    """Timeout: a PGM pode ter emitido a guia sem responder; não repete."""
    rotas = responder(runner, monkeypatch, [(200, INDISPONIVEL)])

    runner.run_emitir_guia_happy_paths()

    assert rotas == ["/emitir_guia"]
    assert sleeps == []
    saida = capsys.readouterr().out
    assert "emitir_guia happy path: SKIPPED - PGM answered 'indisponivel'" in saida
    assert "in 1 attempt(s)" in saida
    assert "FAIL" not in saida


def test_skip_do_emitir_guia_nao_pula_o_regularizacao(runner, monkeypatch, sleeps):
    monkeypatch.setattr(runner, "REGULARIZACAO_PAYLOAD", json.dumps(PAYLOAD_AVISTA))
    rotas = responder(
        runner,
        monkeypatch,
        [(200, SEM_REGISTROS)] * 3 + [(200, SUCESSO)],
    )

    runner.run_emitir_guia_happy_paths()

    assert rotas == ["/emitir_guia"] * 3 + ["/emitir_guia_regularizacao"]


def test_erro_generico_reprova_sem_nova_tentativa(runner, monkeypatch, sleeps, capsys):
    """
    "Tente novamente mais tarde" é o `except Exception` da rota: pode ser uma
    exceção do nosso código depois de a PGM emitir a guia, então não repete
    (nova emissão real) nem vira skip.
    """
    rotas = responder(runner, monkeypatch, [(200, ERRO_GENERICO)])

    with pytest.raises(SystemExit) as excinfo:
        runner.run_emitir_guia_happy_paths()

    assert excinfo.value.code == 1
    assert rotas == ["/emitir_guia"]
    assert sleeps == []
    saida = capsys.readouterr().out
    assert "FAIL: emitir_guia happy path: expected api_resposta_sucesso=true" in saida
    assert '"upstream_failure": "erro_generico"' in saida
    assert "Tente novamente" not in saida


def test_erro_desconhecido_reprova_sem_nova_tentativa(
    runner, monkeypatch, sleeps, capsys
):
    rotas = responder(runner, monkeypatch, [(200, ERRO_DE_NEGOCIO)])

    with pytest.raises(SystemExit) as excinfo:
        runner.run_emitir_guia_happy_paths()

    assert excinfo.value.code == 1
    assert rotas == ["/emitir_guia"]
    assert sleeps == []
    saida = capsys.readouterr().out
    assert "FAIL: emitir_guia happy path" in saida
    assert '"api_resposta_sucesso": false' in saida
    assert '"api_descricao_erro"' in saida  # a chave, não o valor
    assert_sem_dados_da_resposta(saida)


def test_http_diferente_de_200_reprova_sem_nova_tentativa(
    runner, monkeypatch, sleeps, capsys
):
    rotas = responder(runner, monkeypatch, [(500, SEM_REGISTROS)])

    with pytest.raises(SystemExit):
        runner.run_emitir_guia_happy_paths()

    assert rotas == ["/emitir_guia"]
    saida = capsys.readouterr().out
    assert "expected HTTP 200, got 500" in saida
    assert '"http_status": 500' in saida
    assert_sem_dados_da_resposta(saida)


def test_sucesso_sem_codigo_de_barras_reprova(runner, monkeypatch, sleeps, capsys):
    """O contrato do sucesso não afrouxou."""
    sem_codigo = {k: v for k, v in SUCESSO.items() if k != "codigo_de_barras"}
    responder(runner, monkeypatch, [(200, sem_codigo)])

    with pytest.raises(SystemExit):
        runner.run_emitir_guia_happy_paths()

    saida = capsys.readouterr().out
    assert "missing key 'codigo_de_barras'" in saida
    assert_sem_dados_da_resposta(saida)


def test_skip_do_regularizacao_nao_imprime_a_descricao(
    runner, monkeypatch, sleeps, capsys
):
    monkeypatch.setattr(runner, "AVISTA_PAYLOAD", "")
    monkeypatch.setattr(runner, "REGULARIZACAO_PAYLOAD", json.dumps(PAYLOAD_AVISTA))
    stale = {
        "api_resposta_sucesso": False,
        "api_descricao_erro": (
            f"Não há parcelas em atraso para a execução {EXECUCAO_FISCAL_FALSA}"
        ),
    }
    responder(runner, monkeypatch, [(200, stale)])

    runner.run_emitir_guia_happy_paths()

    saida = capsys.readouterr().out
    assert "SKIPPED - fixture stale" in saida
    assert EXECUCAO_FISCAL_FALSA not in saida


def test_consulta_falha_imprime_so_o_resumo(runner, monkeypatch, capsys):
    monkeypatch.setattr(runner, "CONSULTA_TIPO", "cpfCnpj")
    monkeypatch.setattr(runner, "CONSULTA_VALOR", "00000000000")
    responder(runner, monkeypatch, [(200, ERRO_DE_NEGOCIO)])

    with pytest.raises(SystemExit):
        runner.run_consulta_happy_path()

    saida = capsys.readouterr().out
    assert "consulta_debitos happy path" in saida
    assert_sem_dados_da_resposta(saida)


def test_payload_invalido_do_segredo_nao_e_impresso(runner, monkeypatch, capsys):
    monkeypatch.setattr(runner, "AVISTA_PAYLOAD", json.dumps([EXECUCAO_FISCAL_FALSA]))

    with pytest.raises(SystemExit):
        runner.run_emitir_guia_happy_paths()

    saida = capsys.readouterr().out
    assert "PREVIEW_AVISTA_PAYLOAD: expected JSON object" in saida
    assert EXECUCAO_FISCAL_FALSA not in saida


@pytest.mark.parametrize(
    ("resposta", "esperado"),
    [
        (SEM_REGISTROS, "sem_registros"),
        (
            {
                "api_resposta_sucesso": False,
                "api_descricao_erro": "NAO FORAM ENCONTRADOS REGISTROS",
            },
            "sem_registros",
        ),
        (INDISPONIVEL, "indisponivel"),
        (ERRO_GENERICO, "erro_generico"),
        (ERRO_DE_NEGOCIO, None),
        (SUCESSO, None),
        ({**SEM_REGISTROS, "api_resposta_sucesso": None}, None),
        ({"api_resposta_sucesso": False, "api_descricao_erro": ["x"]}, None),
        (["não foram encontrados registros"], None),
        (None, None),
    ],
)
def test_classify_upstream_failure(runner, resposta, esperado):
    assert runner.classify_upstream_failure(resposta) == esperado


def test_response_summary_so_tem_status_chaves_e_tamanho(runner):
    resumo = runner.response_summary(200, json.dumps(SUCESSO), SUCESSO)

    assert resumo == {
        "http_status": 200,
        "api_resposta_sucesso": True,
        "keys": sorted(SUCESSO),
    }
    assert runner.response_summary(502, "<html>bad gateway</html>", None) == {
        "http_status": 502,
        "json_type": "none",
        "body_length": len("<html>bad gateway</html>"),
    }
    assert runner.response_summary(raw="[1, 2]", parsed=[1, 2]) == {
        "json_type": "list",
        "length": 2,
    }
