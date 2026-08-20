"""CLI de reprocessamento manual da DLQ (CHATR-126).

O worker automático resolve o caso comum, mas para de propósito diante de erro
transitório — e é aí que alguém precisa de uma ferramenta: conferir o que está
parado sem consumir a fila, e reprocessar na hora depois de corrigir a causa.

O que estes testes prendem: `--help` funciona sem o ambiente da aplicação (o
import do módulo pesado é adiado justamente para isso), `--dry-run` não escreve
nada, e a saída não engole erro — fila que não escoou tem que virar código de
saída não-zero para o job que chamou perceber.
"""

import json
import sys
import types

import pytest

from src.utils import bq_dlq_replay


@pytest.fixture
def bigquery_falso(monkeypatch):
    """Substitui `src.utils.bigquery` pelas duas funções que o CLI usa."""
    chamadas = {"replay": [], "depth": 0}

    def _replay_bigquery_dlq(limite=None, table_full_name=None, dry_run=False):
        chamadas["replay"].append((limite, table_full_name, dry_run))
        return {
            "itens": 3,
            "linhas": 7,
            "poison": 1,
            "pendentes": 2,
            "erros": [],
        }

    def _get_dlq_depth():
        chamadas["depth"] += 1
        return {"redis": 4, "poison": 1, "arquivos": 2, "total": 7}

    monkeypatch.setitem(
        sys.modules,
        "src.utils.bigquery",
        types.SimpleNamespace(
            replay_bigquery_dlq=_replay_bigquery_dlq,
            get_dlq_depth=_get_dlq_depth,
        ),
    )
    return chamadas


def test_help_nao_exige_o_ambiente_da_aplicacao():
    """O import pesado é adiado de propósito — `--help` tem que rodar sempre."""
    with pytest.raises(SystemExit) as exc:
        bq_dlq_replay.main(["--help"])
    assert exc.value.code == 0


def test_dry_run_e_repassado(bigquery_falso, capsys):
    assert bq_dlq_replay.main(["--dry-run"]) == 0
    assert bigquery_falso["replay"] == [(None, None, True)]
    assert "[dry-run]" in capsys.readouterr().out


def test_limite_e_tabela_sao_repassados(bigquery_falso):
    bq_dlq_replay.main(["--limit", "500", "--table", "proj.ds.tbl"])
    assert bigquery_falso["replay"] == [(500, "proj.ds.tbl", False)]


def test_saida_json_e_parseavel(bigquery_falso, capsys):
    """A saída em JSON existe para ser consumida por script, não por gente."""
    bq_dlq_replay.main(["--json"])
    resultado = json.loads(capsys.readouterr().out)
    assert resultado["itens"] == 3
    assert resultado["pendentes"] == 2


def test_depth_only_nao_reprocessa(bigquery_falso, capsys):
    assert bq_dlq_replay.main(["--depth-only"]) == 0
    assert bigquery_falso["replay"] == [], "conferir a profundidade consumiu a fila"
    assert bigquery_falso["depth"] == 1
    assert "7" in capsys.readouterr().out


def test_erro_transitorio_vira_codigo_de_saida_nao_zero(monkeypatch, capsys):
    """Fila que não escoou não pode passar por execução bem-sucedida."""
    monkeypatch.setitem(
        sys.modules,
        "src.utils.bigquery",
        types.SimpleNamespace(
            replay_bigquery_dlq=lambda **_k: {
                "itens": 0,
                "linhas": 0,
                "poison": 0,
                "pendentes": 5,
                "erros": ["proj.ds.tbl: 503 Service Unavailable"],
            },
            get_dlq_depth=lambda: {},
        ),
    )
    assert bq_dlq_replay.main([]) == 1
    assert "503" in capsys.readouterr().err


def test_item_em_poison_nao_falha_a_execucao(monkeypatch):
    """Poison saiu da fila com sucesso — precisa de correção, não de retry."""
    monkeypatch.setitem(
        sys.modules,
        "src.utils.bigquery",
        types.SimpleNamespace(
            replay_bigquery_dlq=lambda **_k: {
                "itens": 0,
                "linhas": 0,
                "poison": 3,
                "pendentes": 0,
                "erros": [],
            },
            get_dlq_depth=lambda: {},
        ),
    )
    assert bq_dlq_replay.main([]) == 0
