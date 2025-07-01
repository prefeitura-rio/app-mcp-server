from src.utils.infisical import getenv_or_action

DUMMY_API_URL = getenv_or_action("DUMMY_API_URL")

VALID_TOKENS = getenv_or_action("VALID_TOKENS").split(",")