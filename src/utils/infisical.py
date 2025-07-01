# -*- coding: utf-8 -*-
from os import getenv
from typing import List, Dict
from pathlib import Path
from loguru import logger


_env_cache: Dict[str, str] = {}


def _load_dotenv() -> Dict[str, str]:
    """Carrega variáveis do arquivo .env na raiz do projeto.

    Returns:
        Dict[str, str]: Dicionário com as variáveis do arquivo .env
    """
    global _env_cache

    if _env_cache:
        return _env_cache

    env_path = Path(".env")
    if not env_path.exists():
        return {}

    env_vars = {}
    with open(env_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            # Processa linhas com formato KEY=VALUE
            if "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()

                # Remove aspas se existirem
                if value and len(value) >= 2:
                    if (value[0] == value[-1] == '"') or (value[0] == value[-1] == "'"):
                        value = value[1:-1]

                env_vars[key] = value

    _env_cache = env_vars
    return env_vars


def getenv_or_action(
    env_name: str, *, action: str = "raise", default: str = None
) -> str:
    """Get an environment variable or raise an exception.

    Args:
        env_name (str): The name of the environment variable.
        action (str, optional): The action to take if the environment variable is not set.
            Defaults to "raise".
        default (str, optional): The default value to return if the environment variable is not set.
            Defaults to None.

    Raises:
        ValueError: If the action is not one of "raise", "warn", or "ignore".

    Returns:
        str: The value of the environment variable, or the default value if the environment variable
            is not set.
    """
    if action not in ["raise", "warn", "ignore"]:
        raise ValueError("action must be one of 'raise', 'warn', or 'ignore'")

    # Tenta obter a variável do ambiente
    value = getenv(env_name, None)

    # Se não encontrar, tenta obter do arquivo .env
    if value is None:
        env_vars = _load_dotenv()
        value = env_vars.get(env_name, default)

    # Se ainda não encontrou, aplica a ação especificada
    if value is None:
        if action == "raise":
            raise EnvironmentError(f"Environment variable {env_name} is not set.")
        elif action == "warn":
            logger.warning(f"Warning: Environment variable {env_name} is not set.")
    return value


def getenv_list_or_action(
    env_name: str, *, action: str = "raise", default: str = None
) -> List[str]:
    """Get an environment variable or raise an exception.

    Args:
        env_name (str): The name of the environment variable.
        action (str, optional): The action to take if the environment variable is not set.
            Defaults to "raise".
        default (str, optional): The default value to return if the environment variable is not set.
            Defaults to None.

    Raises:
        ValueError: If the action is not one of "raise", "warn", or "ignore".

    Returns:
        str: The value of the environment variable, or the default value if the environment variable
            is not set.
    """
    value = getenv_or_action(env_name, action=action, default=default)
    if value is not None:
        if isinstance(value, str):
            return value.split(",")
        elif isinstance(value, list):
            return value
        else:
            raise TypeError("value must be a string or a list")
    return []


def mask_string(string: str, *, mask: str = "*") -> str:
    """This function masks a string with a given mask.
    It will show a few first and last characters of the string, and mask the rest.
    The number of characters shown is the length of the string divided by 4, rounded down.

    Args:
        string (str): The string to be masked.
        mask (str, optional): The mask to use. Defaults to "*".

    Returns:
        str: The masked string.
    """
    length = len(string)
    number_of_characters_to_show = int(length / 4)
    if number_of_characters_to_show % 2 == 0:
        number_of_starting_characters_to_show = int(number_of_characters_to_show / 2)
        number_of_ending_characters_to_show = number_of_starting_characters_to_show
    else:
        number_of_starting_characters_to_show = int(number_of_characters_to_show / 2)
        number_of_ending_characters_to_show = number_of_starting_characters_to_show + 1
    first_characters = string[:number_of_starting_characters_to_show]
    last_characters = string[-number_of_ending_characters_to_show:]
    return f"{first_characters}{mask * (length - number_of_characters_to_show * 2)}{last_characters}"  # noqa
