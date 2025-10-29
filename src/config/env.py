import os
from src.utils.infisical import getenv_or_action

# if file .env exists, load it
if os.path.exists("src/config/.env"):
    import dotenv

    dotenv.load_dotenv(dotenv_path="src/config/.env")


VALID_TOKENS = getenv_or_action("VALID_TOKENS")
IS_LOCAL = getenv_or_action("IS_LOCAL", default="false", action="ignore") == "true"
GEMINI_API_KEY = getenv_or_action("GEMINI_API_KEY", action="ignore")

GOOGLE_MAPS_API_URL = getenv_or_action("GOOGLE_MAPS_API_URL")
GOOGLE_MAPS_API_KEY = getenv_or_action("GOOGLE_MAPS_API_KEY")

ENVIRONMENT = getenv_or_action("ENVIRONMENT", default="staging", action="ignore")

GCP_SERVICE_ACCOUNT_CREDENTIALS = getenv_or_action(
    "GCP_SERVICE_ACCOUNT_CREDENTIALS", action="raise"
)
GOOGLE_BIGQUERY_PAGE_SIZE = int(
    getenv_or_action("GOOGLE_BIGQUERY_PAGE_SIZE", default="100")
)
NOMINATIM_API_URL = getenv_or_action("NOMINATIM_API_URL")

SURKAI_API_KEY = getenv_or_action("SURKAI_API_KEY", action="ignore")
DHARMA_API_KEY = getenv_or_action("DHARMA_API_KEY", action="ignore")

# OAuth2 Configuration for RMI API
RMI_API_URL = getenv_or_action("RMI_API_URL", action="ignore")
RMI_OAUTH_ISSUER = getenv_or_action("RMI_OAUTH_ISSUER", action="ignore")
RMI_OAUTH_CLIENT_ID = getenv_or_action("RMI_OAUTH_CLIENT_ID", action="ignore")
RMI_OAUTH_CLIENT_SECRET = getenv_or_action("RMI_OAUTH_CLIENT_SECRET", action="ignore")
RMI_OAUTH_SCOPES = getenv_or_action(
    "RMI_OAUTH_SCOPES", default="profile email", action="ignore"
)

LINK_BLACKLIST = getenv_or_action("LINK_BLACKLIST", default="").split(",")

# Configuração para temas válidos da ferramenta de equipamentos
EQUIPMENTS_VALID_THEMES = getenv_or_action(
    "EQUIPMENTS_VALID_THEMES", default="cultura,saude,educacao,geral"
).split(",")

# PGM API Configuration
CHATBOT_INTEGRATIONS_URL = getenv_or_action("CHATBOT_INTEGRATIONS_URL", action="ignore")
CHATBOT_INTEGRATIONS_KEY = getenv_or_action("CHATBOT_INTEGRATIONS_KEY", action="ignore")
CHATBOT_PGM_API_URL = getenv_or_action("CHATBOT_PGM_API_URL", action="ignore")
CHATBOT_PGM_ACCESS_KEY = getenv_or_action("CHATBOT_PGM_ACCESS_KEY", action="ignore")


# IPTU API Configuration
IPTU_API_URL = getenv_or_action("IPTU_API_URL")
IPTU_API_TOKEN = getenv_or_action("IPTU_API_TOKEN")

SHORT_API_URL = getenv_or_action("SHORT_API_URL")
SHORT_API_TOKEN = getenv_or_action("SHORT_API_TOKEN")

WA_IPTU_URL = getenv_or_action("WA_IPTU_URL")
WA_IPTU_TOKEN = getenv_or_action("WA_IPTU_TOKEN")
WA_IPTU_PUBLIC_KEY = getenv_or_action("WA_IPTU_PUBLIC_KEY")

WORKFLOWS_GCP_SERVICE_ACCOUNT = getenv_or_action("WORKFLOWS_GCP_SERVICE_ACCOUNT")

DIVIDA_ATIVA_API_URL = getenv_or_action("DIVIDA_ATIVA_API_URL")
DIVIDA_ATIVA_ACCESS_KEY = getenv_or_action("DIVIDA_ATIVA_ACCESS_KEY")
REDIS_URL = getenv_or_action("REDIS_URL")
REDIS_TTL_SECONDS = int(getenv_or_action("REDIS_TTL_SECONDS"))


PROXY_URL = getenv_or_action("PROXY_URL")
