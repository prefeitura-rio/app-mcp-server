"""Validações de configuração executadas ANTES da aplicação subir.

Este módulo é deliberadamente restrito a verificações determinísticas e
locais — nada aqui faz I/O de rede. A regra é: se uma nova tentativa não pode
consertar o problema, ele deve derrubar o processo agora, com a lista
completa de erros, em vez de virar um 500 na primeira chamada de tool.

Falhas de conectividade (Redis fora do ar, BigQuery inacessível) não
pertencem aqui: são transitórias, e derrubar o pod por causa delas
transformaria degradação parcial em indisponibilidade total. Essas são
reportadas em `/health/detail`.

Restrição de import: este módulo NÃO pode importar `src.config.env`. O
objetivo é justamente relatar todas as variáveis faltantes de uma vez, e o
import de `src.config.env` levanta `EnvironmentError` na primeira que faltar.
Por isso lemos o ambiente via `getenv_or_action`, que não tem essa dependência.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from src.utils.infisical import getenv_or_action
from src.utils.log import logger

# `src/config/env.py` carrega este arquivo no import; como o preflight roda
# antes dele, precisa enxergar as mesmas variáveis — caso contrário, quem usa
# `.env` local veria o preflight reprovar tudo. `load_dotenv` não sobrescreve
# variáveis já presentes no ambiente, então a precedência não muda.
if os.path.exists("src/config/.env"):
    import dotenv

    dotenv.load_dotenv(dotenv_path="src/config/.env")

# Variáveis sem `default` e sem `action="ignore"/"warn"` em src/config/env.py,
# ou seja, aquelas cujo import já derruba o processo — só que uma de cada vez.
# `src/tests/unit/health/test_required_env_sync.py` faz o parse de env.py e
# garante que esta lista não saia de sincronia.
REQUIRED_ENV_VARS: tuple[str, ...] = (
    "DATA_DIR",
    "DIVIDA_ATIVA_ACCESS_KEY",
    "DIVIDA_ATIVA_API_URL",
    "ERROR_INTERCEPTOR_TOKEN",
    "ERROR_INTERCEPTOR_URL",
    "GCP_SERVICE_ACCOUNT_CREDENTIALS",
    "GMAPS_API_TOKEN",
    "GOOGLE_MAPS_API_KEY",
    "GOOGLE_MAPS_API_URL",
    "IPTU_API_TOKEN",
    "IPTU_API_URL",
    "NOMINATIM_API_URL",
    "PROXY_URL",
    "REDIS_TTL_SECONDS",
    "REDIS_URL",
    "SGRC_AUTHORIZATION_HEADER",
    "SGRC_BODY_TOKEN",
    "SGRC_URL",
    "SHORT_API_TOKEN",
    "SHORT_API_URL",
    "TYPESENSE_PARAMETERS",
    "VALID_TOKENS",
    "WA_IPTU_PUBLIC_KEY",
    "WA_IPTU_TOKEN",
    "WA_IPTU_URL",
    "WORKFLOWS_GCP_SERVICE_ACCOUNT",
    "WORKFLOWS_GCS_BUCKET",
)

# Arquivos lidos por src/tools/multi_step_service/workflows/poda_de_arvore.
REQUIRED_DATA_FILES: tuple[str, ...] = ("bairros.json", "logradouros.json")

# (valor bruto da credencial, veredito) — ver `check_gcp_credentials`.
_gcp_credentials_verdict: Optional[Tuple[str, Optional[str]]] = None


def _get(name: str) -> Optional[str]:
    """Lê uma variável de ambiente sem levantar exceção."""
    return getenv_or_action(name, action="ignore")


def check_required_env() -> List[str]:
    """Retorna TODAS as variáveis obrigatórias ausentes ou vazias.

    Diferente do import de `src.config.env`, que aborta na primeira, aqui a
    lista é completa: o operador descobre tudo em um deploy, não em N.
    """
    missing = [name for name in REQUIRED_ENV_VARS if not (_get(name) or "").strip()]
    return [
        f"Variável de ambiente obrigatória ausente ou vazia: {name}" for name in missing
    ]


def check_gcp_credentials() -> Optional[str]:
    """Valida que `GCP_SERVICE_ACCOUNT_CREDENTIALS` produz credenciais válidas.

    Reproduz exatamente o que `src.utils.bigquery.get_gcp_credentials()` faz
    (base64 → JSON → `from_service_account_info`), mas no boot. Sem essa
    verificação, uma credencial malformada só se manifesta na primeira
    chamada de BigQuery. Nenhuma chamada de rede é feita.

    O veredito é memoizado pelo valor da credencial: o parse da chave RSA
    custa ~40ms de CPU, e o health check em runtime chama esta função de
    dentro do event loop. Como mesma entrada dá mesmo veredito, e a rotação
    de credencial pelo Infisical reinicia o pod, o cache nunca fica obsoleto.
    """
    global _gcp_credentials_verdict

    raw = _get("GCP_SERVICE_ACCOUNT_CREDENTIALS")
    if not raw:
        return None  # já reportado por check_required_env

    if _gcp_credentials_verdict is not None and _gcp_credentials_verdict[0] == raw:
        return _gcp_credentials_verdict[1]

    verdict = _validate_gcp_credentials(raw)
    _gcp_credentials_verdict = (raw, verdict)
    return verdict


def reset_gcp_credentials_cache() -> None:
    """Descarta o veredito memoizado (usado nos testes)."""
    global _gcp_credentials_verdict
    _gcp_credentials_verdict = None


def _validate_gcp_credentials(raw: str) -> Optional[str]:
    # `binascii.Error` e `json.JSONDecodeError` derivam de `ValueError`.
    try:
        decoded = base64.b64decode(raw, validate=True)
    except ValueError:
        return "GCP_SERVICE_ACCOUNT_CREDENTIALS não é base64 válido"

    try:
        info = json.loads(decoded)
    except ValueError:
        return "GCP_SERVICE_ACCOUNT_CREDENTIALS não contém JSON válido após decodificar base64"

    if not isinstance(info, dict):
        return "GCP_SERVICE_ACCOUNT_CREDENTIALS deve conter um objeto JSON de service account"

    # Import tardio: mantém o preflight barato quando a validação nem roda.
    from google.oauth2 import service_account

    try:
        service_account.Credentials.from_service_account_info(info)
    except Exception as exc:
        # A mensagem do google-auth cita apenas nomes de campos ausentes,
        # nunca o valor da chave privada.
        return f"GCP_SERVICE_ACCOUNT_CREDENTIALS não é uma service account utilizável: {exc}"

    return None


def check_valid_tokens() -> Optional[str]:
    """Garante que sobra pelo menos um token estático utilizável.

    `"".split(",")` devolve `[""]`, então uma variável vazia — ou com uma
    entrada vazia, como em `"a,,b"` — introduziria um token vazio no set do
    `HybridTokenVerifier`. Sem token válido, 100% das requisições retornam 401.
    """
    raw = _get("VALID_TOKENS")
    if not raw:
        return None  # já reportado por check_required_env

    tokens = [token.strip() for token in raw.split(",")]
    if any(not token for token in tokens):
        return "VALID_TOKENS contém entrada vazia (vírgulas consecutivas ou no final)"
    return None


def check_data_files() -> List[str]:
    """Verifica que os arquivos geográficos existem e são legíveis."""
    raw_dir = _get("DATA_DIR")
    if not raw_dir:
        return []  # já reportado por check_required_env

    data_dir = Path(raw_dir)
    if not data_dir.is_dir():
        return [f"DATA_DIR não é um diretório acessível: {data_dir}"]

    errors = []
    for filename in REQUIRED_DATA_FILES:
        path = data_dir / filename
        if not path.is_file():
            errors.append(f"Arquivo de dados obrigatório ausente: {path}")
        elif not os.access(path, os.R_OK):
            errors.append(f"Arquivo de dados sem permissão de leitura: {path}")
    return errors


def check_redis_url() -> Optional[str]:
    """Valida que `REDIS_URL` é parseável — sem abrir conexão.

    O parse é determinístico (erro de configuração); a conectividade é
    transitória e fica a cargo do check de runtime.
    """
    raw = _get("REDIS_URL")
    if not raw:
        return None  # já reportado por check_required_env

    try:
        import redis.asyncio as redis
    except ImportError:
        return "Biblioteca 'redis' não instalada"

    try:
        redis.Redis.from_url(raw)
    except Exception as exc:
        # Não interpolamos `exc` nem a URL: mensagens de erro de parse
        # costumam ecoar a URL inteira, senha inclusive.
        return f"REDIS_URL malformada ({type(exc).__name__})"
    return None


def collect_preflight_errors() -> List[str]:
    """Roda todas as validações e devolve a lista consolidada de erros."""
    errors: List[str] = []
    errors.extend(check_required_env())
    errors.extend(check_data_files())
    for check in (check_gcp_credentials, check_valid_tokens, check_redis_url):
        error = check()
        if error:
            errors.append(error)
    return errors


def run_startup_preflight() -> None:
    """Executa o preflight e aborta o processo se houver erro de configuração.

    Chamado por `src/main.py` antes de importar `src.app`. Sair com código 1
    é o comportamento desejado sob Argo Rollouts: o pod novo não sobe, o
    rollout é abortado e a versão estável continua servindo.
    """
    errors = collect_preflight_errors()
    if not errors:
        logger.info("Preflight de configuração OK.")
        return

    formatted = "\n".join(f"  - {error}" for error in errors)
    logger.error(
        f"Preflight de configuração falhou com {len(errors)} erro(s). "
        f"A aplicação NÃO será iniciada:\n{formatted}"
    )
    sys.exit(1)
