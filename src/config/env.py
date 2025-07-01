from src.utils.infisical import getenv_or_action

VALID_TOKENS = getenv_or_action("VALID_TOKENS").split(",")