"""Guarda contra drift entre `REQUIRED_ENV_VARS` e `src/config/env.py`.

A lista do preflight duplica, por necessidade, o conjunto de variáveis que
`src/config/env.py` marca como obrigatórias — o preflight não pode importar
esse módulo, já que o objetivo é justamente reportar todas as faltantes antes
que ele aborte na primeira. Este teste garante que a duplicação não se
desatualize: se alguém tornar uma variável obrigatória (ou deixar de tornar)
em env.py sem mexer no preflight, o teste aponta exatamente qual.
"""

import ast
from pathlib import Path

from src.health.preflight import REQUIRED_ENV_VARS

ENV_MODULE = Path(__file__).resolve().parents[3] / "config" / "env.py"


def _required_vars_declared_in_env_module() -> set[str]:
    """Extrai de env.py as variáveis sem `default` e sem `action` tolerante.

    Essas são exatamente as que derrubam o import quando ausentes, porque o
    `action` padrão de `getenv_or_action` é `"raise"`.
    """
    tree = ast.parse(ENV_MODULE.read_text(encoding="utf-8"))
    required = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "getenv_or_action":
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue

        keywords = {kw.arg: kw.value for kw in node.keywords}
        if "default" in keywords:
            continue

        action = keywords.get("action")
        if isinstance(action, ast.Constant) and action.value in ("ignore", "warn"):
            continue

        required.add(node.args[0].value)

    return required


def test_required_env_vars_espelha_config_env():
    declared = _required_vars_declared_in_env_module()
    listed = set(REQUIRED_ENV_VARS)

    assert declared == listed, (
        "REQUIRED_ENV_VARS saiu de sincronia com src/config/env.py.\n"
        f"  obrigatórias em env.py mas ausentes do preflight: {sorted(declared - listed)}\n"
        f"  no preflight mas não mais obrigatórias em env.py: {sorted(listed - declared)}"
    )


def test_lista_nao_esta_vazia():
    # Protege contra o parse silenciosamente parar de encontrar as chamadas.
    assert len(REQUIRED_ENV_VARS) > 20
