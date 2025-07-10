import os
from src.utils.infisical import getenv_or_action

# if file .env exists, load it
if os.path.exists("src/config/.env"):
    import dotenv

    dotenv.load_dotenv(dotenv_path="src/config/.env")


VALID_TOKENS = getenv_or_action("VALID_TOKENS")
IS_LOCAL = getenv_or_action("IS_LOCAL", default="false", action="ignore") == "true"
