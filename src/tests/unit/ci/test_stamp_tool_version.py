"""Passo "Stamp tool version of this commit" do build-docker (DEV-794).

A imagem de um merge era construída da árvore desse merge, onde
``src/utils/tool_version.json`` ainda registra o merge anterior: o bump do
próprio merge só entra num commit seguinte do update-tool-version, que nunca
vira imagem. O ``_tool_metadata.version`` de staging ficava sempre um deploy
atrás. Estes testes rodam ``scripts/ci/stamp_tool_version.sh`` num repositório
git local e conferem que o build-docker o chama antes do ``docker build``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = PROJECT_ROOT / "scripts" / "ci" / "stamp_tool_version.sh"
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "build-docker.yaml"
VERSION_FILE = "src/utils/tool_version.json"

PREVIOUS_VERSION = {
    "version": "v0000000",
    "last_updated": "2026-01-01T00:00:00Z",
    "description": "Tool version for cache invalidation",
}
MERGE_DATE = "2026-11-24T12:12:00-03:00"
MERGE_DATE_UTC = "2026-11-24T15:12:00Z"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="precisa de git e bash",
)


def _env(home: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
        and key not in {"SOURCE_SHA", "GITHUB_SHA", "OUTPUT", "TZ"}
    }
    env.update(
        {
            "HOME": str(home),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Teste",
            "GIT_AUTHOR_EMAIL": "teste@example.invalid",
            "GIT_COMMITTER_NAME": "Teste",
            "GIT_COMMITTER_EMAIL": "teste@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
            # Fuso diferente de UTC: a data gravada tem de sair em UTC.
            "TZ": "America/Sao_Paulo",
        }
    )
    return env


class Repo:
    """Checkout do runner no SHA do merge, como o build-docker o recebe."""

    def __init__(self, tmp_path: Path) -> None:
        self.env = _env(tmp_path)
        self.path = tmp_path / "runner"
        self.path.mkdir()
        self.git("init", "--quiet", "-b", "main")
        (self.path / "src" / "utils").mkdir(parents=True)
        (self.path / VERSION_FILE).write_text(
            json.dumps(PREVIOUS_VERSION, indent=2) + "\n"
        )
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "chore: bump do merge anterior")
        (self.path / "src" / "app.txt").write_text("v2\n")
        self.git("add", ".")
        self.commit_at(MERGE_DATE, "fix: merge na main")
        self.merge_sha = self.git("rev-parse", "HEAD")

    def git(self, *args: str, env: dict[str, str] | None = None) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.path,
            env=env or self.env,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def commit_at(self, date: str, message: str) -> None:
        env = dict(self.env, GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        self.git("commit", "--quiet", "-m", message, env=env)

    def run_script(self, **extra: str) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        env.update(extra)
        return subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=self.path,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def version(self, path: str = VERSION_FILE) -> dict[str, str]:
        return json.loads((self.path / path).read_text())


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


def test_grava_a_versao_e_a_data_do_proprio_merge(repo: Repo):
    result = repo.run_script(GITHUB_SHA=repo.merge_sha)

    assert result.returncode == 0, result.stderr
    short = repo.git("rev-parse", "--short", repo.merge_sha)
    assert repo.version() == {
        "version": f"v{short}",
        "last_updated": MERGE_DATE_UTC,
        "description": "Tool version for cache invalidation",
    }
    assert f"v{short}" in result.stdout


def test_source_sha_tem_precedencia_sobre_github_sha(repo: Repo):
    previous = repo.git("rev-parse", "HEAD~1")

    result = repo.run_script(SOURCE_SHA=previous, GITHUB_SHA=repo.merge_sha)

    assert result.returncode == 0, result.stderr
    short = repo.git("rev-parse", "--short", previous)
    assert repo.version()["version"] == f"v{short}"


def test_sem_variaveis_usa_o_head(repo: Repo):
    result = repo.run_script()

    assert result.returncode == 0, result.stderr
    assert repo.version()["version"] == (f"v{repo.git('rev-parse', '--short', 'HEAD')}")


def test_nao_commita_nem_move_o_head(repo: Repo):
    result = repo.run_script(GITHUB_SHA=repo.merge_sha)

    assert result.returncode == 0, result.stderr
    assert repo.git("rev-parse", "HEAD") == repo.merge_sha
    assert repo.git("status", "--porcelain") == f"M {VERSION_FILE}"


def test_e_idempotente(repo: Repo):
    first = repo.run_script(GITHUB_SHA=repo.merge_sha)
    content = (repo.path / VERSION_FILE).read_text()

    second = repo.run_script(GITHUB_SHA=repo.merge_sha)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert (repo.path / VERSION_FILE).read_text() == content


def test_output_grava_em_outro_arquivo(repo: Repo):
    result = repo.run_script(GITHUB_SHA=repo.merge_sha, OUTPUT="out/versao.json")

    assert result.returncode == 0, result.stderr
    assert repo.version("out/versao.json")["last_updated"] == MERGE_DATE_UTC
    assert repo.version() == PREVIOUS_VERSION


@pytest.mark.parametrize("sha", ["nao-existe", "f" * 40, "$(touch x)"])
def test_recusa_sha_invalido_sem_tocar_o_arquivo(repo: Repo, sha: str):
    result = repo.run_script(SOURCE_SHA=sha)

    assert result.returncode != 0
    assert "SOURCE_SHA não é um commit" in result.stderr
    assert repo.version() == PREVIOUS_VERSION
    assert not (repo.path / "x").exists()


def test_workflow_grava_a_versao_do_sha_do_push_antes_do_build():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["build"]["steps"]
    names = [step.get("name") for step in steps]
    stamp = names.index("Stamp tool version of this commit")
    build = names.index("Build and tag Docker image")

    assert names.index("Checkout code") < stamp < build
    step = steps[stamp]
    assert step["run"].strip() == "bash scripts/ci/stamp_tool_version.sh"
    assert step["env"]["SOURCE_SHA"] == "${{ github.sha }}"
    assert "if" not in step
    assert "continue-on-error" not in step
    # Mesmo tamanho de SHA curto que o update-tool-version grava na main.
    checkout = steps[names.index("Checkout code")]
    assert checkout["with"]["fetch-depth"] == 0
    # A imagem continua tagueada pelo SHA do merge que o deploy-staging espera.
    tags = steps[names.index("Extract metadata for Docker tags")]["run"]
    assert "COMMIT_HASH=${{ github.sha }}" in tags
