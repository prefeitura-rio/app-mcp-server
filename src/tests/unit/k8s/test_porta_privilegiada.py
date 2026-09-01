"""Trava o par `capabilities` × porta do container nos dois ambientes.

Por que este teste existe:

O `securityContext` com `capabilities.drop: [ALL]` entrou junto com a porta 80,
e as duas coisas se contradizem. O bind em porta abaixo de 1024 é decidido por
capability, não por uid: o kernel chama `ns_capable(CAP_NET_BIND_SERVICE)`, que
falha mesmo para uid 0 quando a capability não está no conjunto efetivo. Com
`drop: [ALL]` e sem `add`, o processo não sobe na porta 80 nem como root —
`permission denied` no bind, e o pod entra em CrashLoopBackOff.

A prova local não acusa, e é essa a armadilha que o teste fecha: o Docker define
`net.ipv4.ip_unprivileged_port_start=0` dentro do container, então qualquer
porta liga sem capability nenhuma. O Kubernetes deixa o sysctl no default 1024
do kernel. Rodar o container na mão passa; o cluster quebra.

Um teste de manifesto pega isso sem precisar de cluster: a regra é estática e
está inteira nos dois YAMLs. `test_porta_do_codigo_bate_com_o_manifesto` fecha o
outro lado — mudar a porta em `src/main.py` sem mexer no manifesto (ou o
contrário) também derruba o pod, e nenhum teste de aplicação veria.
"""

import ast
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[4]

MANIFESTOS = ("k8s/prod/resources.yaml", "k8s/staging/resources.yaml")

# Default do kernel para `net.ipv4.ip_unprivileged_port_start`. O kubelet não
# altera esse sysctl, então é o valor que vale no cluster.
PRIMEIRA_PORTA_LIVRE = 1024

CAP_BIND = "NET_BIND_SERVICE"

# Kinds que carregam um pod template. `Rollout` é o do Argo, que é o que este
# repositório usa; os outros ficam para o dia em que algum manifesto mudar de
# forma, para o teste não passar a ignorar o arquivo em silêncio.
KINDS_COM_POD = {"Rollout", "Deployment", "StatefulSet", "DaemonSet", "Job"}


def _carregar(caminho_relativo):
    caminho = PROJECT_ROOT / caminho_relativo
    with caminho.open(encoding="utf-8") as arquivo:
        return [doc for doc in yaml.safe_load_all(arquivo) if doc]


def _containers_com_porta(caminho_relativo):
    """Devolve `(nome_do_recurso, container)` de todo container do manifesto."""
    achados = []
    for doc in _carregar(caminho_relativo):
        if doc.get("kind") not in KINDS_COM_POD:
            continue
        nome = doc.get("metadata", {}).get("name", "?")
        pod_spec = doc["spec"]["template"]["spec"]
        for container in pod_spec.get("containers", []):
            achados.append((nome, container))
    return achados


def _tem_cap_de_bind(container):
    """Resolve a capability efetiva a partir de `drop` e `add`.

    `drop: [ALL]` zera o conjunto, e só o que estiver em `add` volta. Sem
    `drop: [ALL]`, o default do runtime já inclui `NET_BIND_SERVICE` — a menos
    que ele apareça dropado nominalmente.
    """
    caps = (container.get("securityContext") or {}).get("capabilities") or {}
    drop = {str(item).upper() for item in caps.get("drop") or ()}
    add = {str(item).upper() for item in caps.get("add") or ()}

    if "ALL" in drop:
        return CAP_BIND in add
    if CAP_BIND in drop:
        return CAP_BIND in add
    return True


def _porta_do_main():
    """Extrai a porta passada a `mcp.run()` em `src/main.py`, sem importar."""
    origem = (PROJECT_ROOT / "src" / "main.py").read_text(encoding="utf-8")
    for no in ast.walk(ast.parse(origem)):
        if not isinstance(no, ast.Call):
            continue
        alvo = no.func
        if not (isinstance(alvo, ast.Attribute) and alvo.attr == "run"):
            continue
        for argumento in no.keywords:
            if argumento.arg == "port" and isinstance(argumento.value, ast.Constant):
                return argumento.value.value
    return None


@pytest.mark.parametrize("manifesto", MANIFESTOS)
def test_porta_privilegiada_exige_a_capability_de_bind(manifesto):
    containers = _containers_com_porta(manifesto)
    assert containers, f"{manifesto}: nenhum container encontrado"

    for nome, container in containers:
        portas = [
            porta["containerPort"]
            for porta in container.get("ports") or ()
            if porta.get("containerPort") is not None
        ]
        privilegiadas = [porta for porta in portas if porta < PRIMEIRA_PORTA_LIVRE]
        if not privilegiadas:
            continue
        assert _tem_cap_de_bind(container), (
            f"{manifesto}: o container '{container.get('name', nome)}' escuta em "
            f"{privilegiadas} e não tem {CAP_BIND} efetivo. Com `drop: [ALL]` o "
            f"bind falha com 'permission denied' no cluster (mas passa no Docker, "
            f"que zera net.ipv4.ip_unprivileged_port_start). Acrescente "
            f"{CAP_BIND} em `capabilities.add` ou mova o serviço para uma porta "
            f">= {PRIMEIRA_PORTA_LIVRE}."
        )


@pytest.mark.parametrize("manifesto", MANIFESTOS)
def test_porta_do_codigo_bate_com_o_manifesto(manifesto):
    porta_codigo = _porta_do_main()
    assert porta_codigo is not None, (
        "não achei `port=` em `mcp.run()` de src/main.py — se a porta virou "
        "configurável, este teste precisa acompanhar"
    )

    portas_manifesto = {
        porta["containerPort"]
        for _, container in _containers_com_porta(manifesto)
        for porta in container.get("ports") or ()
    }
    assert porta_codigo in portas_manifesto, (
        f"src/main.py escuta em {porta_codigo}, mas {manifesto} declara "
        f"{sorted(portas_manifesto)}. A probe bate na porta do manifesto: "
        f"divergir derruba o rollout."
    )
