"""Passo "Commit resolved image to git" do deploy de staging (DEV-412).

O deploy grava a imagem implantada em ``k8s/staging/resources.yaml`` e publica
o commit na ``main``. Depois de cada merge, o ``update-tool-version.yaml``
empurra outro commit na ``main`` enquanto o deploy espera a imagem, e o push
direto do checkout do SHA implantado saía non-fast-forward, engolido por
``|| echo "Nothing to push"``. Estes testes rodam o script
``scripts/ci/commit_staging_image.sh`` contra um remoto git local, com a
corrida reproduzida de verdade.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = PROJECT_ROOT / "scripts" / "ci" / "commit_staging_image.sh"
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "deploy-staging.yaml"
MANIFEST = "k8s/staging/resources.yaml"

OLD_IMAGE = "ghcr.io/prefeitura-rio/app-mcp-server:" + "a" * 40 + "@sha256:" + "1" * 64
NEW_IMAGE = "ghcr.io/prefeitura-rio/app-mcp-server:" + "b" * 40 + "@sha256:" + "2" * 64
MANIFEST_TEMPLATE = """apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata:
  name: mcp
spec:
  template:
    spec:
      containers:
        - name: mcp
          image: {image}  # Updated by deploy-staging workflow
          ports:
            - containerPort: 80
"""

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="precisa de git e bash",
)


def _env(home: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") and key not in {"FULL_IMAGE", "GITHUB_SHA"}
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
    """Remoto bare, o checkout do runner no SHA implantado e um outro clone."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.env = _env(tmp_path)
        self.remote = tmp_path / "remote.git"
        self.runner = tmp_path / "runner"
        self.other = tmp_path / "other"

        self.git(tmp_path, "init", "--quiet", "--bare", "-b", "main", str(self.remote))
        seed = tmp_path / "seed"
        self.git(tmp_path, "clone", "--quiet", str(self.remote), str(seed))
        (seed / "k8s" / "staging").mkdir(parents=True)
        (seed / MANIFEST).write_text(MANIFEST_TEMPLATE.format(image=OLD_IMAGE))
        (seed / "app.txt").write_text("v1\n")
        self.git(seed, "add", ".")
        self.git(seed, "commit", "--quiet", "-m", "feat: merge implantado")
        self.git(seed, "push", "--quiet", "origin", "HEAD:main")
        self.deployed_sha = self.git(seed, "rev-parse", "HEAD")

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

    def advance_main(self, name: str) -> str:
        """Publica na main um commit de outro workflow (o update-tool-version)."""
        self.git(self.other, "pull", "--quiet", "--ff-only", "origin", "main")
        (self.other / name).write_text(f"{name}\n")
        self.git(self.other, "add", name)
        self.git(self.other, "commit", "--quiet", "-m", f"chore: {name}")
        self.git(self.other, "push", "--quiet", "origin", "HEAD:main")
        return self.git(self.other, "rev-parse", "HEAD")

    def run_script(self, **extra: str) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        env.update(
            {
                "FULL_IMAGE": NEW_IMAGE,
                "GITHUB_SHA": self.deployed_sha,
                "RETRY_DELAY": "0",
            }
        )
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

    def remote_log(self) -> list[str]:
        return self.git(self.remote, "log", "--format=%s", "main").splitlines()


@pytest.fixture
def repos(tmp_path: Path) -> Repos:
    return Repos(tmp_path)


def test_publica_o_manifesto_quando_a_main_andou_depois_do_checkout(repos: Repos):
    tool_version_sha = repos.advance_main("tool_version")

    result = repos.run_script()

    assert result.returncode == 0, result.stderr
    short = repos.git(repos.runner, "rev-parse", "--short", repos.deployed_sha)
    assert repos.remote_log()[0] == f"chore: update staging image to {short}"
    assert repos.git(repos.remote, "rev-parse", "main~1") == tool_version_sha
    assert f"image: {NEW_IMAGE}  # Updated by deploy-staging workflow" in (
        repos.remote_file(MANIFEST)
    )
    assert repos.remote_file("tool_version") == "tool_version"
    # O checkout do SHA implantado (usado depois pelos E2E) fica intacto.
    assert repos.git(repos.runner, "rev-parse", "HEAD") == repos.deployed_sha
    assert OLD_IMAGE in (repos.runner / MANIFEST).read_text()
    assert repos.git(repos.runner, "status", "--porcelain") == ""
    assert repos.git(repos.runner, "worktree", "list").count("\n") == 0


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
    assert "Tentativa 2/5" in result.stdout
    log = repos.remote_log()
    assert log[0].startswith("chore: update staging image to ")
    assert log[1] == "chore: concorrente"
    assert NEW_IMAGE in repos.remote_file(MANIFEST)


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


def test_nao_cria_commit_quando_o_manifesto_ja_tem_a_imagem(repos: Repos):
    first = repos.run_script()
    assert first.returncode == 0, first.stderr
    head = repos.git(repos.remote, "rev-parse", "main")

    second = repos.run_script()

    assert second.returncode == 0, second.stderr
    assert "nada a commitar" in second.stdout
    assert repos.git(repos.remote, "rev-parse", "main") == head


@pytest.mark.parametrize(
    "image",
    [
        "",
        "ghcr.io/outra-org/app-mcp-server:abc@sha256:" + "2" * 64,
        "ghcr.io/prefeitura-rio/app-mcp-server:abc",
        "ghcr.io/prefeitura-rio/app-mcp-server:abc|x@sha256:" + "2" * 64,
    ],
)
def test_recusa_full_image_invalida_sem_tocar_a_main(repos: Repos, image: str):
    before = repos.git(repos.remote, "rev-parse", "main")

    result = repos.run_script(FULL_IMAGE=image)

    assert result.returncode != 0
    assert "FULL_IMAGE inválida" in result.stderr
    assert repos.git(repos.remote, "rev-parse", "main") == before


def test_falha_quando_o_manifesto_nao_tem_a_linha_de_imagem(repos: Repos):
    seed = repos.other
    repos.git(seed, "pull", "--quiet", "--ff-only", "origin", "main")
    (seed / MANIFEST).write_text("apiVersion: v1\nkind: ConfigMap\n")
    repos.git(seed, "commit", "--quiet", "-am", "chore: sem imagem")
    repos.git(seed, "push", "--quiet", "origin", "HEAD:main")
    before = repos.git(repos.remote, "rev-parse", "main")

    result = repos.run_script()

    assert result.returncode != 0
    assert "não tem a linha de imagem" in result.stderr
    assert repos.git(repos.remote, "rev-parse", "main") == before


def test_workflow_usa_o_script_e_nao_engole_falha_de_push():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["deploy"]["steps"]
    names = [step.get("name") for step in steps]
    step = steps[names.index("Commit resolved image to git")]
    run = step["run"]

    assert "bash scripts/ci/commit_staging_image.sh" in run
    assert "|| echo" not in run
    assert "git push" not in run
    # O manifesto é gravado antes de o deploy mexer no Application.
    assert names.index("Commit resolved image to git") < names.index(
        "Update Argo CD Application image override"
    )
