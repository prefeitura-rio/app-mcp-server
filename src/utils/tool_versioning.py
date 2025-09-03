"""
Sistema de versionamento para tools MCP
"""

import json
from typing import Any, Dict
from pathlib import Path
import subprocess
from datetime import datetime
import json
import argparse

UTILS_PATH = Path("src/utils")
VERSION_PLACEHOLDER = {
    "version": "vERROR",
    "last_updated": datetime.now().isoformat(),
    "description": "Tool version for cache invalidation",
}


def get_git_commit_hash():
    """Obtém o hash do commit atual"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=".",
        )
        if result.returncode == 0:
            return result.stdout.strip()
        else:
            return None
    except Exception:
        return None


def get_current_version():
    """Obtém a versão atual do arquivo"""
    try:
        version_file = UTILS_PATH / "tool_version.json"
        if version_file.exists():
            with open(version_file, "r") as f:
                version_data = json.load(f)
                return version_data
        else:
            return VERSION_PLACEHOLDER
    except Exception:
        return VERSION_PLACEHOLDER


def update_version():
    """Atualiza a versão no arquivo tool_version.json usando o hash do commit git"""

    # Usar hash do commit atual
    commit_hash = get_git_commit_hash()
    if not commit_hash:
        print("Erro: Não foi possível obter o hash do commit git")
        return False

    version = f"v{commit_hash}"

    # Atualizar arquivo
    version_file = UTILS_PATH / "tool_version.json"
    version_data = {
        "version": version,
        "last_updated": datetime.now().isoformat(),
        "description": "Tool version for cache invalidation",
    }

    try:
        with open(version_file, "w") as f:
            json.dump(version_data, f, indent=2)

        print(f"✅ Versão atualizada para: {version}")
        print(f"📝 Arquivo atualizado: {version_file}")
        return True

    except Exception as e:
        print(f"❌ Erro ao atualizar arquivo: {e}")
        return False


def get_tool_version_from_file() -> dict:
    """Retorna versão armazenada no arquivo tool_version.json"""

    try:
        # Encontrar o arquivo tool_version.json no diretório raiz do projeto
        version_file = UTILS_PATH / "tool_version.json"

        if version_file.exists():
            with open(version_file, "r") as f:
                version_data = json.load(f)
                return version_data
        else:
            return VERSION_PLACEHOLDER
    except Exception:
        return VERSION_PLACEHOLDER


def add_tool_version(response: Any) -> Dict[str, Any]:
    """
    Adiciona metadados de versionamento à resposta da tool.

    Args:
        response: Resposta original da tool
        tool_name: Nome da tool para identificação

    Returns:
        Dict com resposta original + metadados de versão
    """
    # Usar versão armazenada no arquivo JSON
    tool_version_data = get_tool_version_from_file()

    # Estruturar resposta com metadados
    versioned_response = {
        "_tool_metadata": tool_version_data,
        "data": response,
    }

    return versioned_response


def main():
    parser = argparse.ArgumentParser(description="Atualiza a versão das tools do MCP")
    parser.add_argument(
        "--show", "-s", action="store_true", help="Apenas mostrar a versão atual"
    )

    args = parser.parse_args()

    if args.show:
        current = get_current_version()
        print(f"Versão atual: {current}")
        return

    success = update_version()
    if not success:
        exit(1)


if __name__ == "__main__":
    main()
