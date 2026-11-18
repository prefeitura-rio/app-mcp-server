"""Aviso do Discord do deploy de staging e da release (DEV-629).

O payload era montado num heredoc com a mensagem e o autor do commit crus:
aspa dupla ou barra invertida (todo revert tem ``Revert "..."``) deixava o
JSON inválido, o Discord respondia 400 e o ``curl -f`` derrubava o job de um
deploy que tinha dado certo. Estes testes rodam o script
``scripts/ci/discord_notify.sh`` e o próprio passo dos workflows contra um
servidor HTTP local que faz o papel do webhook.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = PROJECT_ROOT / "scripts" / "ci" / "discord_notify.sh"
WORKFLOWS = PROJECT_ROOT / ".github" / "workflows"
STEP_NAME = "Send Discord notification"

# Sintético: aspas, barra invertida, acentos, emoji e sintaxe de shell.
HOSTILE_MSG = (
    'Revert "fix(equipamentos): não envia \\ o endereço" $(echo x) `id` ção 🚀'
)
HOSTILE_AUTHOR = 'Zé "Teste" \\ da Silva'

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None
    or shutil.which("jq") is None
    or shutil.which("curl") is None,
    reason="precisa de bash, jq e curl",
)


class _Webhook:
    """Webhook falso: guarda o corpo de cada POST e responde ``status``."""

    def __init__(self) -> None:
        self.status = 204
        self.requests: list[tuple[str, bytes]] = []
        webhook = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                webhook.requests.append((self.headers.get("Content-Type", ""), body))
                self.send_response(webhook.status)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/webhook"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def payloads(self) -> list[dict]:
        return [json.loads(body) for _, body in self.requests]


@pytest.fixture
def webhook() -> Iterator[_Webhook]:
    hook = _Webhook()
    hook.thread.start()
    try:
        yield hook
    finally:
        hook.server.shutdown()
        hook.server.server_close()


def _closed_port_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/webhook"


def _env(**extra: str) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("DISCORD_") and not key.startswith("GIT_")
    }
    env.update(extra)
    return env


def _run(args: list[str], **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=_env(**env),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


BASE_ARGS = [
    "--title",
    '✅ Staging Deploy "Succeeded"',
    "--description",
    "Preview tests passed \\ promoted.",
    "--color",
    "3066993",
    "--link-field",
    "Commit",
    HOSTILE_MSG,
    "https://github.com/org/repo/commit/abc",
    "false",
    "--field",
    "Author",
    HOSTILE_AUTHOR,
    "true",
    "--field",
    "Version",
    "`abc1234`",
    "true",
]


def test_payload_com_aspas_barra_e_acentos_e_json_valido() -> None:
    result = _run(BASE_ARGS, DISCORD_DRY_RUN="1")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    embed = payload["embeds"][0]
    assert embed["title"] == '✅ Staging Deploy "Succeeded"'
    assert embed["description"] == "Preview tests passed \\ promoted."
    assert embed["color"] == 3066993
    assert embed["footer"] == {"text": "GitHub Actions"}
    commit, author, version = embed["fields"]
    assert commit == {
        "name": "Commit",
        "value": f"[{HOSTILE_MSG[:50]}](https://github.com/org/repo/commit/abc)",
        "inline": False,
    }
    assert author == {"name": "Author", "value": HOSTILE_AUTHOR, "inline": True}
    assert version == {"name": "Version", "value": "`abc1234`", "inline": True}


def test_link_corta_por_caractere_sem_quebrar_acento() -> None:
    text = "ç" * 60
    result = _run(
        [
            "--title",
            "t",
            "--color",
            "1",
            "--link-field",
            "Commit",
            text,
            "https://x.invalid/c",
            "false",
        ],
        DISCORD_DRY_RUN="1",
    )

    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)["embeds"][0]["fields"][0]["value"]
    assert value == f"[{'ç' * 50}](https://x.invalid/c)"


def test_envia_json_valido_ao_webhook(webhook: _Webhook) -> None:
    result = _run(BASE_ARGS, DISCORD_WEBHOOK=webhook.url)

    assert result.returncode == 0, result.stderr
    assert len(webhook.requests) == 1
    content_type, _ = webhook.requests[0]
    assert content_type == "application/json"
    embed = webhook.payloads()[0]["embeds"][0]
    assert embed["fields"][1]["value"] == HOSTILE_AUTHOR
    assert "::warning::" not in result.stdout


def test_discord_400_nao_falha(webhook: _Webhook) -> None:
    webhook.status = 400

    result = _run(BASE_ARGS, DISCORD_WEBHOOK=webhook.url)

    assert result.returncode == 0, result.stderr
    assert len(webhook.requests) == 1
    assert "::warning::Discord notification failed" in result.stdout
    # A URL do webhook é segredo e não aparece no aviso.
    assert webhook.url not in result.stdout + result.stderr


def test_webhook_inalcancavel_nao_falha() -> None:
    url = _closed_port_url()

    result = _run(BASE_ARGS, DISCORD_WEBHOOK=url, DISCORD_TIMEOUT="5")

    assert result.returncode == 0, result.stderr
    assert "::warning::Discord notification failed" in result.stdout
    assert url not in result.stdout + result.stderr


def test_sem_webhook_pula_o_aviso() -> None:
    result = _run(BASE_ARGS)

    assert result.returncode == 0, result.stderr
    assert "skipping notification" in result.stdout


@pytest.mark.parametrize(
    "args",
    [
        ["--title", "t", "--color", "verde"],
        ["--title", "t", "--color", "1", "--field", "n", "v", "sim"],
        ["--title", "t", "--color", "1", "--field", "n"],
        ["--color", "1"],
        ["--title", "t", "--color", "1", "--desconhecido"],
    ],
)
def test_argumento_invalido_sai_com_2(args: list[str]) -> None:
    result = _run(args, DISCORD_DRY_RUN="1")

    assert result.returncode == 2
    assert "::error::" in result.stderr
    assert result.stdout == ""


def _discord_step(workflow: str) -> dict:
    data = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    steps = [
        step
        for job in data["jobs"].values()
        for step in job.get("steps", [])
        if step.get("name") == STEP_NAME
    ]
    assert len(steps) == 1, f"{workflow}: esperado um passo {STEP_NAME!r}"
    return steps[0]


@pytest.mark.parametrize("workflow", ["deploy-staging.yaml", "release.yaml"])
def test_passo_do_workflow_usa_o_script_e_nao_reprova_o_job(workflow: str) -> None:
    step = _discord_step(workflow)
    run = step["run"]

    assert step.get("continue-on-error") is True
    assert step.get("if") == "always()"
    assert "scripts/ci/discord_notify.sh" in run
    # Nada de montar o JSON à mão nem de chamar o curl direto.
    assert "<<EOF" not in run
    assert "curl" not in run
    assert "${{" not in run


def _commit_repo(tmp_path: Path, message: str, author: str) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    (repo / "scripts" / "ci").mkdir(parents=True)
    shutil.copy(SCRIPT, repo / "scripts" / "ci" / SCRIPT.name)
    git_env = _env(
        HOME=str(tmp_path),
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME=author,
        GIT_AUTHOR_EMAIL="teste@example.invalid",
        GIT_COMMITTER_NAME="Teste",
        GIT_COMMITTER_EMAIL="teste@example.invalid",
    )
    for cmd in (
        ["git", "init", "-q"],
        ["git", "add", "."],
        ["git", "commit", "-q", "-m", message],
    ):
        subprocess.run(cmd, cwd=repo, env=git_env, check=True, timeout=30)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        env=git_env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()
    return repo, sha


def _run_step(
    workflow: str, tmp_path: Path, step_env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Roda o ``run`` do passo como o runner roda (bash -e -o pipefail)."""
    step = _discord_step(workflow)
    script = tmp_path / "step.sh"
    script.write_text(step["run"], encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    kubectl = bin_dir / "kubectl"
    kubectl.write_text("#!/usr/bin/env bash\necho Healthy\n", encoding="utf-8")
    kubectl.chmod(0o755)
    env = _env(
        HOME=str(tmp_path),
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        **step_env,
    )
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", str(script)],
        cwd=step_env["REPO_DIR"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize("status", [204, 400])
def test_passo_do_deploy_staging_com_commit_de_revert(
    tmp_path: Path, webhook: _Webhook, status: int
) -> None:
    webhook.status = status
    repo, sha = _commit_repo(tmp_path, HOSTILE_MSG, HOSTILE_AUTHOR)

    result = _run_step(
        "deploy-staging.yaml",
        tmp_path,
        {
            "REPO_DIR": str(repo),
            "DISCORD_WEBHOOK": webhook.url,
            "GITHUB_SHA": sha,
            "GITHUB_REPOSITORY": "org/repo",
            "JOB_STATUS": "success",
            "HAS_PREVIEW": "true",
            "K8S_ONLY": "false",
            "FULL_IMAGE": "ghcr.io/org/repo:abc@sha256:" + "0" * 64,
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    embed = webhook.payloads()[0]["embeds"][0]
    assert embed["title"] == "✅ Staging Deploy Succeeded"
    names = [field["name"] for field in embed["fields"]]
    assert names == ["Commit", "Author", "Version", "Image", "Preview", "K8s-only"]
    assert embed["fields"][0]["value"] == (
        f"[{HOSTILE_MSG[:50]}](https://github.com/org/repo/commit/{sha})"
    )
    assert embed["fields"][1]["value"] == HOSTILE_AUTHOR
    assert embed["fields"][4]["value"] == "`true`"
    if status == 400:
        assert "::warning::Discord notification failed" in result.stdout


@pytest.mark.parametrize("status", [204, 400])
def test_passo_da_release_com_commit_de_revert(
    tmp_path: Path, webhook: _Webhook, status: int
) -> None:
    webhook.status = status
    repo, sha = _commit_repo(tmp_path, HOSTILE_MSG, HOSTILE_AUTHOR)

    result = _run_step(
        "release.yaml",
        tmp_path,
        {
            "REPO_DIR": str(repo),
            "DISCORD_WEBHOOK": webhook.url,
            "GITHUB_SHA": sha,
            "GITHUB_REPOSITORY": "org/repo",
            "VERSION": "v1.2.3",
            "COMMIT_HASH": sha[:7],
            "ROLLOUT_NAME": "mcp",
            "K8S_NAMESPACE": "mcp",
            "FULL_IMAGE": "ghcr.io/org/repo:v1.2.3@sha256:" + "0" * 64,
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    embed = webhook.payloads()[0]["embeds"][0]
    assert embed["title"] == "✅ Production Release Successful"
    values = {field["name"]: field["value"] for field in embed["fields"]}
    assert values["🏷️ Version"] == (
        "[v1.2.3](https://github.com/org/repo/releases/tag/v1.2.3)"
    )
    assert values["📊 Status"] == "`Healthy`"
    assert values["📝 Commit"] == (
        f"[{HOSTILE_MSG[:50]}](https://github.com/org/repo/commit/{sha})"
    )
    assert values["👤 Author"] == HOSTILE_AUTHOR
    if status == 400:
        assert "::warning::Discord notification failed" in result.stdout
