import os
from src.utils.infisical import getenv_or_action

# if file .env exists, load it
if os.path.exists("src/config/.env"):
    import dotenv

    dotenv.load_dotenv(dotenv_path="src/config/.env")


ENVIRONMENT = getenv_or_action("ENVIRONMENT", default="staging", action="ignore")
VALID_TOKENS = getenv_or_action("VALID_TOKENS")
IS_LOCAL = getenv_or_action("IS_LOCAL", default="false", action="ignore") == "true"

WORKFLOWS_GCP_SERVICE_ACCOUNT = getenv_or_action("WORKFLOWS_GCP_SERVICE_ACCOUNT")
WORKFLOWS_GCS_BUCKET = getenv_or_action("WORKFLOWS_GCS_BUCKET")

GEMINI_API_KEY = getenv_or_action("GEMINI_API_KEY", action="ignore")
GEMINI_MODEL = getenv_or_action("GEMINI_MODEL", default="gemini-2.5-flash", action="ignore")

GOOGLE_MAPS_API_URL = getenv_or_action("GOOGLE_MAPS_API_URL")
GOOGLE_MAPS_API_KEY = getenv_or_action("GOOGLE_MAPS_API_KEY")

SHORT_API_URL = getenv_or_action("SHORT_API_URL")
SHORT_API_TOKEN = getenv_or_action("SHORT_API_TOKEN")

GCP_SERVICE_ACCOUNT_CREDENTIALS = getenv_or_action("GCP_SERVICE_ACCOUNT_CREDENTIALS", action="raise")
GOOGLE_BIGQUERY_PAGE_SIZE = int(getenv_or_action("GOOGLE_BIGQUERY_PAGE_SIZE", default="100"))
NOMINATIM_API_URL = getenv_or_action("NOMINATIM_API_URL")

SURKAI_API_KEY = getenv_or_action("SURKAI_API_KEY", action="ignore")
DHARMA_API_KEY = getenv_or_action("DHARMA_API_KEY", action="ignore")

TYPESENSE_HUB_SEARCH_URL = getenv_or_action("TYPESENSE_HUB_SEARCH_URL", action="ignore")

# Error Interceptor Configuration
ERROR_INTERCEPTOR_URL = getenv_or_action("ERROR_INTERCEPTOR_URL")
ERROR_INTERCEPTOR_TOKEN = getenv_or_action("ERROR_INTERCEPTOR_TOKEN")

# OAuth2 Configuration for RMI API
RMI_API_URL = getenv_or_action("RMI_API_URL", action="ignore")
RMI_OAUTH_ISSUER = getenv_or_action("RMI_OAUTH_ISSUER", action="ignore")
RMI_OAUTH_CLIENT_ID = getenv_or_action("RMI_OAUTH_CLIENT_ID", action="ignore")
RMI_OAUTH_CLIENT_SECRET = getenv_or_action("RMI_OAUTH_CLIENT_SECRET", action="ignore")
RMI_OAUTH_SCOPES = getenv_or_action("RMI_OAUTH_SCOPES", default="profile email", action="ignore")

LINK_BLACKLIST = getenv_or_action("LINK_BLACKLIST", default="").split(",")

# Configuração para temas válidos da ferramenta de equipamentos
EQUIPMENTS_VALID_THEMES = getenv_or_action(
    "EQUIPMENTS_VALID_THEMES",
    default="cultura,saude,educacao,geral,assistencia_social,incidentes_hidricos",
)

# Configuração para excluir ferramentas do servidor MCP
# Lista de nomes de ferramentas separados por vírgula (ex: "calculator_add,google_search")
EXCLUDED_TOOLS = list(
    set(
        tool.strip()
        for tool in getenv_or_action(
            "EXCLUDED_TOOLS", default="user_feedback", action="ignore"
        ).split(",")
        if tool.strip()
    )
)

# PGM API Configuration
CHATBOT_INTEGRATIONS_URL = getenv_or_action("CHATBOT_INTEGRATIONS_URL", action="ignore")
CHATBOT_INTEGRATIONS_KEY = getenv_or_action("CHATBOT_INTEGRATIONS_KEY", action="ignore")
CHATBOT_PGM_API_URL = getenv_or_action("CHATBOT_PGM_API_URL", action="ignore")
CHATBOT_PGM_ACCESS_KEY = getenv_or_action("CHATBOT_PGM_ACCESS_KEY", action="ignore")

# IPTU API Configuration
IPTU_API_URL = getenv_or_action("IPTU_API_URL")
IPTU_API_TOKEN = getenv_or_action("IPTU_API_TOKEN")
WA_IPTU_URL = getenv_or_action("WA_IPTU_URL")
WA_IPTU_TOKEN = getenv_or_action("WA_IPTU_TOKEN")
WA_IPTU_PUBLIC_KEY = getenv_or_action("WA_IPTU_PUBLIC_KEY")

# Dívida Ativa API Configuration
DIVIDA_ATIVA_API_URL = getenv_or_action("DIVIDA_ATIVA_API_URL")
DIVIDA_ATIVA_ACCESS_KEY = getenv_or_action("DIVIDA_ATIVA_ACCESS_KEY")

REDIS_URL = getenv_or_action("REDIS_URL")
REDIS_TTL_SECONDS = int(getenv_or_action("REDIS_TTL_SECONDS"))

PROXY_URL = getenv_or_action("PROXY_URL")

## EAI-Engine
MCP_SERVER_URL = getenv_or_action("MCP_SERVER_URL", action="ignore")
MCP_API_TOKEN = getenv_or_action("MCP_API_TOKEN", action="ignore")

EAI_AGENT_URL = getenv_or_action("EAI_AGENT_URL", action="ignore")
EAI_AGENT_TOKEN = getenv_or_action("EAI_AGENT_TOKEN", action="ignore")

EAI_GATEWAY_API_URL = getenv_or_action("EAI_GATEWAY_API_URL", action="ignore")
EAI_GATEWAY_API_TOKEN = getenv_or_action("EAI_GATEWAY_API_TOKEN", action="ignore")

PROJECT_ID = getenv_or_action("PROJECT_ID", action="ignore")
LOCATION = getenv_or_action("LOCATION", action="ignore")
INSTANCE = getenv_or_action("INSTANCE", action="ignore")
DATABASE = getenv_or_action("DATABASE", action="ignore")
DATABASE_USER = getenv_or_action("DATABASE_USER", action="ignore")
DATABASE_PASSWORD = getenv_or_action("DATABASE_PASSWORD", action="ignore")
GCS_BUCKET = getenv_or_action("GCS_BUCKET", action="ignore")

PROJECT_NUMBER = getenv_or_action("PROJECT_NUMBER", action="ignore")
REASONING_ENGINE_ID = getenv_or_action("REASONING_ENGINE_ID", action="ignore")

OTEL_EXPORTER_OTLP_TRACES_ENDPOINT = getenv_or_action("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", action="ignore")
OTEL_EXPORTER_OTLP_TRACES_HEADERS = getenv_or_action("OTEL_EXPORTER_OTLP_TRACES_HEADERS", action="ignore")

# Short-term memory limits (kept as strings for deployment)
SHORT_MEMORY_TIME_LIMIT = getenv_or_action("SHORT_MEMORY_TIME_LIMIT", default="30")  # in days
SHORT_MEMORY_TOKEN_LIMIT = getenv_or_action("SHORT_MEMORY_TOKEN_LIMIT", default="50000")  # in tokens

# SGRC Configuration
SGRC_URL = getenv_or_action("SGRC_URL")
SGRC_AUTHORIZATION_HEADER = getenv_or_action("SGRC_AUTHORIZATION_HEADER")
SGRC_BODY_TOKEN = getenv_or_action("SGRC_BODY_TOKEN")
GMAPS_API_TOKEN = getenv_or_action("GMAPS_API_TOKEN")
DATA_DIR = getenv_or_action("DATA_DIR")

TYPESENSE_ACTIVE = getenv_or_action("TYPESENSE_ACTIVE", default="false", action="ignore").lower() == "true"