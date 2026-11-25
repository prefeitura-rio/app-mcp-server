"""Passo "Commit and push version update" do update-tool-version (DEV-641).

O job grava em ``src/utils/tool_version.json`` a versão do merge que o disparou
e publica o commit na ``main``. Entre o checkout e o push, a ``main`` pode
andar (outro merge, o ``chore: update staging image`` do deploy de staging), e o
``git push`` direto do checkout saía non-fast-forward, perdendo a versão daquele
merge. Estes testes rodam o script ``scripts/ci/commit_tool_version.sh`` contra
um remoto git local, com a corrida reproduzida de verdade.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = PROJECT_ROOT / "scripts" / "ci" / "commit_tool_version.sh"
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "update-tool-version.yaml"
VERSION_FILE = "src/utils/tool_version.json"

OLD_VERSION = {
    "version": "v0000000",
    "last_updated": "2026-01-01T00:00:00Z",
    "description": "Tool version for cache invalidation",
}

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="precisa de git e bash",
)


def _env(home: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") and key not in {"SOURCE_SHA", "GITHUB_SHA"}
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
        }
    )
    return env


class Repos:
    """Remoto bare, o checkout do runner no SHA do merge e um outro clone."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.env = _env(tmp_path)
        self.remote = tmp_path / "remote.git"
        self.runner = tmp_path / "runner"
        self.other = tmp_path / "other"

        self.git(tmp_path, "init", "--quiet", "--bare", "-b", "main", str(self.remote))
        seed = tmp_path / "seed"
        self.git(tmp_path, "clone", "--quiet", str(self.remote), str(seed))
        (seed / "src" / "utils").mkdir(parents=True)
        (seed / VERSION_FILE).write_text(json.dumps(OLD_VERSION, indent=2) + "\n")
        (seed / "src" / "app.txt").write_text("v1\n")
        self.git(seed, "add", ".")
        self.git(seed, "commit", "--quiet", "-m", "feat: merge na main")
        self.git(seed, "push", "--quiet", "origin", "HEAD:main")
        self.merge_sha = self.git(seed, "rev-parse", "HEAD")

        self.git(tmp_path, "clone", "--quiet", str(self.remote), str(self.runner))
        self.git(tmp_path, "clone", "--quiet", str(self.remote), str(self.other))

    def git(self, cwd: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def short(self, sha: str) -> str:
        return self.git(self.runner, "rev-parse", "--short", sha)

    def advance_main(self, name: str, content: str | None = None) -> str:
        """Publica na main um commit de outro workflow ou de outro merge."""
        self.git(self.other, "pull", "--quiet", "--ff-only", "origin", "main")
        path = self.other / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content if content is not None else f"{name}\n")
        self.git(self.other, "add", name)
        self.git(self.other, "commit", "--quiet", "-m", f"chore: {name}")
        self.git(self.other, "push", "--quiet", "origin", "HEAD:main")
        return self.git(self.other, "rev-parse", "HEAD")

    def run_script(self, **extra: str) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        env.update({"GITHUB_SHA": self.merge_sha, "RETRY_DELAY": "0"})
        env.update(extra)
        return subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=self.runner,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def remote_file(self, path: str) -> str:
        return self.git(self.remote, "show", f"main:{path}")

    def remote_version(self) -> dict[str, str]:
        return json.loads(self.remote_file(VERSION_FILE))

    def remote_log(self) -> list[str]:
        return self.git(self.remote, "log", "--format=%s", "main").splitlines()


@pytest.fixture
def repos(tmp_path: Path) -> Repos:
    return Repos(tmp_path)


def test_publica_a_versao_quando_a_main_andou_depois_do_checkout(repos: Repos):
    staging_sha = repos.advance_main("k8s/staging/resources.yaml", "image: x\n")

    result = repos.run_script()

    assert result.returncode == 0, result.stderr
    short = repos.short(repos.merge_sha)
    assert repos.remote_log()[0] == f"chore: update tool version to {short} 🤖"
    assert repos.git(repos.remote, "rev-parse", "main~1") == staging_sha
    version = repos.remote_version()
    assert version["version"] == f"v{short}"
    assert version["description"] == "Tool version for cache invalidation"
    assert version["last_updated"].endswith("Z")
    assert repos.remote_file("k8s/staging/resources.yaml") == "image: x"
    # O checkout do runner fica intacto.
    assert repos.git(repos.runner, "rev-parse", "HEAD") == repos.merge_sha
    assert json.loads((repos.runner / VERSION_FILE).read_text()) == OLD_VERSION
    assert repos.git(repos.runner, "status", "--porcelain") == ""
    assert repos.git(repos.runner, "worktree", "list").count("\n") == 0


def test_publica_a_versao_sem_corrida(repos: Repos):
    result = repos.run_script()

    assert result.returncode == 0, result.stderr
    assert "Tentativa 1/5" in result.stdout
    assert "Tentativa 2/5" not in result.stdout
    assert repos.git(repos.remote, "rev-parse", "main~1") == repos.merge_sha
    assert repos.remote_version()["version"] == f"v{repos.short(repos.merge_sha)}"


def test_grava_o_mesmo_conteudo_que_a_imagem_do_merge_leva(repos: Repos):
    # DEV-794: o build-docker grava na imagem o que o stamp_tool_version.sh
    # gera para o merge; o arquivo da main tem de ser idêntico a ele.
    stamped = repos.tmp / "imagem.json"
    stamp = subprocess.run(
        ["bash", str(SCRIPT.with_name("stamp_tool_version.sh"))],
        cwd=repos.runner,
        env=dict(repos.env, SOURCE_SHA=repos.merge_sha, OUTPUT=str(stamped)),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert stamp.returncode == 0, stamp.stderr

    result = repos.run_script()

    assert result.returncode == 0, result.stderr
    assert repos.remote_file(VERSION_FILE) == stamped.read_text().strip()
    committed = repos.git(
        repos.runner, "show", "-s", "--format=%cd", "--date=unix", repos.merge_sha
    )
    expected = datetime.fromtimestamp(int(committed), tz=timezone.utc)
    assert repos.remote_version()["last_updated"] == expected.strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def test_refaz_o_push_quando_perde_a_corrida_entre_o_fetch_e_o_push(repos: Repos):
    # O pre-push roda depois do fetch do script e antes do envio: publica um
    # commit concorrente uma vez, e o primeiro push sai non-fast-forward.
    marker = repos.tmp / "corrida-feita"
    hook = repos.runner / ".git" / "hooks" / "pre-push"
    hook.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        f'if [ ! -e "{marker}" ]; then\n'
        f'  touch "{marker}"\n'
        "  unset $(git rev-parse --local-env-vars)\n"
        f'  cd "{repos.other}"\n'
        "  git pull --quiet --ff-only origin main\n"
        "  echo concorrente > concorrente\n"
        "  git add concorrente\n"
        "  git commit --quiet -m 'chore: concorrente'\n"
        "  git push --quiet origin HEAD:main\n"
        "fi\n"
    )
    hook.chmod(0o755)

    result = repos.run_script()

    assert result.returncode == 0, result.stderr
    assert marker.exists()
    assert "recusado; nova tentativa" in result.stdout
    assert "Tentativa 2/5" in result.stdout
    log = repos.remote_log()
    assert log[0].startswith("chore: update tool version to ")
    assert log[1] == "chore: concorrente"
    assert repos.remote_version()["version"] == f"v{repos.short(repos.merge_sha)}"


def test_falha_quando_o_push_e_recusado_em_todas_as_tentativas(repos: Repos):
    hook = repos.remote / "hooks" / "pre-receive"
    hook.write_text("#!/usr/bin/env bash\necho 'recusado pelo teste' >&2\nexit 1\n")
    hook.chmod(0o755)
    before = repos.git(repos.remote, "rev-parse", "main")

    result = repos.run_script(MAX_ATTEMPTS="2")

    assert result.returncode != 0
    assert "recusado em 2 tentativas" in result.stderr
    assert "Tentativa 2/2" in result.stdout
    assert repos.git(repos.remote, "rev-parse", "main") == before


def test_refaz_o_fetch_quando_ele_falha_uma_vez(repos: Repos):
    # Falha passageira do GitHub no fetch: o primeiro upload-pack cai.
    marker = repos.tmp / "fetch-falhou"
    flaky = repos.tmp / "flaky-upload-pack"
    flaky.write_text(
        "#!/usr/bin/env bash\n"
        f'if [ ! -e "{marker}" ]; then touch "{marker}"; exit 1; fi\n'
        'exec git-upload-pack "$@"\n'
    )
    flaky.chmod(0o755)
    repos.git(repos.runner, "config", "remote.origin.uploadpack", str(flaky))

    result = repos.run_script()

    assert result.returncode == 0, result.stderr
    assert marker.exists()
    assert "falhou; nova tentativa" in result.stdout
    assert "Tentativa 2/5" in result.stdout
    assert repos.remote_version()["version"] == f"v{repos.short(repos.merge_sha)}"


def test_falha_quando_o_fetch_falha_em_todas_as_tentativas(repos: Repos):
    repos.git(
        repos.runner, "remote", "set-url", "origin", str(repos.tmp / "nao-existe.git")
    )
    before = repos.git(repos.remote, "rev-parse", "main")

    result = repos.run_script(MAX_ATTEMPTS="2")

    assert result.returncode != 0
    assert "falhou em 2 tentativas" in result.stderr
    assert repos.git(repos.remote, "rev-parse", "main") == before


def test_nao_cria_commit_quando_a_ponta_ja_registra_a_versao(repos: Repos):
    first = repos.run_script()
    assert first.returncode == 0, first.stderr
    head = repos.git(repos.remote, "rev-parse", "main")

    second = repos.run_script()

    assert second.returncode == 0, second.stderr
    assert "nada a commitar" in second.stdout
    assert repos.git(repos.remote, "rev-parse", "main") == head


def test_nao_regride_a_versao_gravada_por_um_merge_posterior(repos: Repos):
    # Dois merges próximos: o job do segundo publica antes do job do primeiro.
    later_merge = repos.advance_main("src/app.txt", "v2\n")
    later_short = repos.git(repos.other, "rev-parse", "--short", later_merge)
    newer = dict(OLD_VERSION, version=f"v{later_short}")
    repos.advance_main(VERSION_FILE, json.dumps(newer, indent=2) + "\n")
    head = repos.git(repos.remote, "rev-parse", "main")

    result = repos.run_script()

    assert result.returncode == 0, result.stderr
    assert f"já registra {later_short}" in result.stdout
    assert "nada a commitar" in result.stdout
    assert repos.git(repos.remote, "rev-parse", "main") == head
    assert repos.remote_version()["version"] == f"v{later_short}"


def test_grava_por_cima_de_versao_que_nao_resolve_para_commit(repos: Repos):
    unknown = dict(OLD_VERSION, version="vdeadbeef")
    repos.advance_main(VERSION_FILE, json.dumps(unknown, indent=2) + "\n")

    result = repos.run_script()

    assert result.returncode == 0, result.stderr
    assert repos.remote_version()["version"] == f"v{repos.short(repos.merge_sha)}"


def test_falha_quando_o_commit_nao_esta_na_main(repos: Repos):
    stray = repos.git(
        repos.runner, "commit-tree", "-m", "fora da main", f"{repos.merge_sha}^{{tree}}"
    )
    before = repos.git(repos.remote, "rev-parse", "main")

    result = repos.run_script(GITHUB_SHA=stray)

    assert result.returncode != 0
    assert "não está em origin/main" in result.stderr
    assert repos.git(repos.remote, "rev-parse", "main") == before


@pytest.mark.parametrize("sha", ["nao-existe", "f" * 40])
def test_recusa_source_sha_invalido_sem_tocar_a_main(repos: Repos, sha: str):
    before = repos.git(repos.remote, "rev-parse", "main")

    result = repos.run_script(SOURCE_SHA=sha)

    assert result.returncode != 0
    assert "SOURCE_SHA não é um commit" in result.stderr
    assert repos.git(repos.remote, "rev-parse", "main") == before


@pytest.mark.parametrize(
    ("name", "value"),
    [("MAX_ATTEMPTS", "0"), ("MAX_ATTEMPTS", "x"), ("RETRY_DELAY", "-1")],
)
def test_recusa_parametros_invalidos(repos: Repos, name: str, value: str):
    before = repos.git(repos.remote, "rev-parse", "main")

    result = repos.run_script(**{name: value})

    assert result.returncode != 0
    assert name in result.stderr
    assert repos.git(repos.remote, "rev-parse", "main") == before


def test_workflow_usa_o_script_e_nao_faz_push_direto():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["update-version"]["steps"]
    names = [step.get("name") for step in steps]
    step = steps[names.index("Commit and push version update")]
    run = step["run"]

    assert "bash scripts/ci/commit_tool_version.sh" in run
    assert "if" not in step
    assert "|| echo" not in run
    assert run.index("git config --local user.email") < run.index(
        "bash scripts/ci/commit_tool_version.sh"
    )
    for other in steps:
        assert "git push" not in other.get("run", "")
        assert "tool_version.json" not in other.get("run", "")
    # O histórico completo é o que permite comparar a versão gravada na ponta.
    checkout = steps[0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["fetch-depth"] == 0
