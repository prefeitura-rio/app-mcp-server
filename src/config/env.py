import os
from pathlib import Path

from src.utils.infisical import getenv_or_action


def getenv_bool(env_name: str, *, default: str, action: str = "ignore") -> bool:
    value = getenv_or_action(env_name, default=default, action=action)
    if value is None or str(value).strip() == "":
        value = default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


# if file .env exists, load it
if os.path.exists("src/config/.env"):
    import dotenv

    dotenv.load_dotenv(dotenv_path="src/config/.env")


ENVIRONMENT = getenv_or_action("ENVIRONMENT", default="staging", action="ignore")
VALID_TOKENS = getenv_or_action("VALID_TOKENS")
IS_LOCAL = getenv_or_action("IS_LOCAL", default="false", action="ignore") == "true"
MCP_STATELESS_HTTP = getenv_bool(
    "MCP_STATELESS_HTTP", default="false" if IS_LOCAL else "true"
)

WORKFLOWS_GCP_SERVICE_ACCOUNT = getenv_or_action("WORKFLOWS_GCP_SERVICE_ACCOUNT")
WORKFLOWS_GCS_BUCKET = getenv_or_action("WORKFLOWS_GCS_BUCKET")

GEMINI_API_KEY = getenv_or_action("GEMINI_API_KEY", action="ignore")
GEMINI_MODEL = getenv_or_action(
    "GEMINI_MODEL", default="gemini-2.5-flash", action="ignore"
)

# Política de retry da busca via Gemini (CHATR-122).
# Erros 503/UNAVAILABLE do Gemini são transitórios: a janela de espera precisa ser larga
# o bastante para atravessar um pico de saturação, mas curta o bastante para o chat.
# Parametrizado por env para permitir recalibrar sem deploy de código.
GEMINI_SEARCH_RETRY_ATTEMPTS = int(
    getenv_or_action("GEMINI_SEARCH_RETRY_ATTEMPTS", default="4", action="ignore")
)
GEMINI_SEARCH_RETRY_BASE_SECONDS = float(
    getenv_or_action("GEMINI_SEARCH_RETRY_BASE_SECONDS", default="2", action="ignore")
)
GEMINI_SEARCH_RETRY_MAX_BACKOFF_SECONDS = float(
    getenv_or_action(
        "GEMINI_SEARCH_RETRY_MAX_BACKOFF_SECONDS", default="16", action="ignore"
    )
)
GEMINI_SEARCH_RETRY_BUDGET_SECONDS = float(
    getenv_or_action(
        "GEMINI_SEARCH_RETRY_BUDGET_SECONDS", default="60", action="ignore"
    )
)

GOOGLE_MAPS_API_URL = getenv_or_action("GOOGLE_MAPS_API_URL")
GOOGLE_MAPS_API_KEY = getenv_or_action("GOOGLE_MAPS_API_KEY")

SHORT_API_URL = getenv_or_action("SHORT_API_URL")
SHORT_API_TOKEN = getenv_or_action("SHORT_API_TOKEN")

GCP_SERVICE_ACCOUNT_CREDENTIALS = getenv_or_action(
    "GCP_SERVICE_ACCOUNT_CREDENTIALS", action="raise"
)
GOOGLE_BIGQUERY_PAGE_SIZE = int(
    getenv_or_action("GOOGLE_BIGQUERY_PAGE_SIZE", default="100")
)
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
RMI_OAUTH_SCOPES = getenv_or_action(
    "RMI_OAUTH_SCOPES", default="profile email", action="ignore"
)

# Gov.br / Identidade Carioca OAuth2 + PKCE Configuration
# Used for citizen authentication flow via WhatsApp
GOVBR_CLIENT_ID = getenv_or_action("GOVBR_CLIENT_ID", action="ignore")
GOVBR_CLIENT_SECRET = getenv_or_action("GOVBR_CLIENT_SECRET", action="ignore")
GOVBR_REDIRECT_URI = getenv_or_action("GOVBR_REDIRECT_URI", action="ignore")
GOVBR_AUTH_URL = getenv_or_action(
    "GOVBR_AUTH_URL",
    default="https://identidade.prefeitura.rio/auth",
    action="ignore",
)
GOVBR_TOKEN_URL = getenv_or_action(
    "GOVBR_TOKEN_URL",
    default="https://identidade.prefeitura.rio/token",
    action="ignore",
)
GOVBR_SCOPE = getenv_or_action(
    "GOVBR_SCOPE", default="openid profile email cpf", action="ignore"
)
# TTL for auth state in Redis (seconds) - default 5 minutes
GOVBR_AUTH_STATE_TTL = int(
    getenv_or_action("GOVBR_AUTH_STATE_TTL", default="300", action="ignore")
)

# Autenticação JWT via Keycloak ("Identidade Carioca") para o servidor MCP
# atuar como Resource Server (novo consumidor: Salesforce via OAuth 2.0).
# Enquanto ausentes, a autenticação permanece 100% baseada em VALID_TOKENS
# (token estático) — ver HybridTokenVerifier em src/middleware/hybrid_verifier.py.
KEYCLOAK_JWKS_URI = getenv_or_action("KEYCLOAK_JWKS_URI", action="ignore")
KEYCLOAK_ISSUER = getenv_or_action("KEYCLOAK_ISSUER", action="ignore")

# Lista de client_ids (azp) autorizados a autenticar via JWT do Keycloak,
# espelhando TRUSTED_SERVICE_CLIENTS do app-rmi. CSV separado por vírgula
# (ex: "salesforce-mcp-client"). Vazia = qualquer client válido do realm.
KEYCLOAK_TRUSTED_CLIENTS = list(
    set(
        client_id.strip()
        for client_id in getenv_or_action(
            "KEYCLOAK_TRUSTED_CLIENTS", default="", action="ignore"
        ).split(",")
        if client_id.strip()
    )
)
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
BIGQUERY_CACHE_TTL_SECONDS = int(
    getenv_or_action("BIGQUERY_CACHE_TTL_SECONDS", default="3600", action="ignore")
)
# Timeout de socket do cliente Redis usado como cache do BigQuery. Baixo de
# propósito: o cache existe para economizar tempo, então esperar por ele mais
# do que a própria query custaria é o pior dos mundos. Um Redis que aceita a
# conexão e não responde precisa falhar rápido e cair para o BigQuery.
REDIS_CACHE_TIMEOUT_SECONDS = float(
    getenv_or_action("REDIS_CACHE_TIMEOUT_SECONDS", default="2.0", action="ignore")
)
# Timeout de socket do cliente Redis síncrono usado pela DLQ de escrita. Mais
# folgado que o do cache porque a escolha aqui é outra: o cache que desiste cedo
# cai para o BigQuery, que é a fonte da verdade, enquanto a DLQ que desiste cedo
# cai para arquivo local — em pod efêmero, isso é perder o registro no próximo
# restart. Vale esperar um pouco mais para gravar no Redis. Mas precisa ter
# teto: sem ele, um Redis particionado prende a thread do executor para sempre
# e o fallback em arquivo nunca chega a rodar.
REDIS_DLQ_TIMEOUT_SECONDS = float(
    getenv_or_action("REDIS_DLQ_TIMEOUT_SECONDS", default="5.0", action="ignore")
)
BIGQUERY_TIMEOUT_SECONDS = float(
    getenv_or_action("BIGQUERY_TIMEOUT_SECONDS", default="10.0", action="ignore")
)
# Tamanho do pool de threads dedicado às leituras do BigQuery. Separado do
# executor default de propósito: uma leitura que estourou o prazo mantém a
# thread ocupada até a query terminar sozinha, e no pool default essas threads
# são as mesmas que gravam log, feedback e alerta do COR. Com pool próprio, a
# leitura lenta só atrapalha leitura.
BIGQUERY_READ_MAX_WORKERS = int(
    getenv_or_action("BIGQUERY_READ_MAX_WORKERS", default="8", action="ignore")
)
BIGQUERY_BATCH_SIZE = int(
    getenv_or_action("BIGQUERY_BATCH_SIZE", default="50", action="ignore")
)
# Intervalo do flush periódico do buffer de escrita. Existe porque o gatilho
# por tamanho (`BIGQUERY_BATCH_SIZE`) sozinho deixa linha parada em memória
# quando o volume é baixo: sem este laço, a última linha de um horário de pouco
# tráfego só chegaria ao BigQuery na próxima rajada. Parametrizado para permitir
# recalibrar o par (tamanho, intervalo) sem deploy de código.
BIGQUERY_FLUSH_INTERVAL_SECONDS = float(
    getenv_or_action("BIGQUERY_FLUSH_INTERVAL_SECONDS", default="30", action="ignore")
)
# Teto de linhas mantidas em memória somando todas as tabelas. É uma trava de
# segurança contra crescimento sem limite: se o flush parar de escoar (BigQuery
# fora, thread morta), o buffer cresceria até estourar a memória do pod — e o
# limite de 1536Mi do container transformaria perda de log em OOMKill do
# servidor inteiro. Ao bater o teto as linhas mais antigas vão para a DLQ, que
# é recuperável, em vez de simplesmente somirem.
BIGQUERY_BATCH_MAX_BUFFERED_ROWS = int(
    getenv_or_action(
        "BIGQUERY_BATCH_MAX_BUFFERED_ROWS", default="10000", action="ignore"
    )
)
# Tamanho do pool de threads dedicado às escritas (log/feedback/alerta e drain
# da DLQ). Mesmo motivo do pool de leitura: o retry com backoff de
# `insert_rows_json_with_retry_and_dlq` segura a thread em `time.sleep`, e no
# executor default essas threads são as mesmas que atendem qualquer outra
# chamada bloqueante do app. Pequeno de propósito — escrita é assíncrona ao
# usuário e não precisa de paralelismo alto.
BIGQUERY_WRITE_MAX_WORKERS = int(
    getenv_or_action("BIGQUERY_WRITE_MAX_WORKERS", default="4", action="ignore")
)
# Teto de tempo de cada chamada de insert. Sem ele, o `insert_rows_json` herda
# o default do transporte e pode não voltar — o que é grave num ponto
# específico: o flush de encerramento roda dentro do handler de sinal, e uma
# chamada pendurada ali segura o processo até o Kubernetes perder a paciência e
# mandar SIGKILL. Aí não sobra nem o que a DLQ salvaria.
BIGQUERY_WRITE_TIMEOUT_SECONDS = float(
    getenv_or_action("BIGQUERY_WRITE_TIMEOUT_SECONDS", default="10.0", action="ignore")
)
# Teto por chamada no flush de encerramento — mais curto que o normal porque o
# orçamento ali é o `terminationGracePeriod` do pod, não a vontade de escrever.
# Uma tentativa que estoura o prazo cai na DLQ, que é recuperável.
BIGQUERY_SHUTDOWN_TIMEOUT_SECONDS = float(
    getenv_or_action(
        "BIGQUERY_SHUTDOWN_TIMEOUT_SECONDS", default="5.0", action="ignore"
    )
)
# Prazo, no encerramento, para o que já foi submetido ao pool de escrita chegar
# ao buffer antes de o flush final acontecer. Sem esta espera, uma linha que
# ficou na fila do executor e ainda não começou a rodar é cancelada junto com o
# pool: ela não está no buffer (logo, o flush não a alcança) nem na DLQ (que só
# recebe o que chegou a falhar no BigQuery). Seria a mesma perda silenciosa que
# o CHATR-103 existe para eliminar, um passo depois de toda a proteção.
#
# Teto próprio, e não `BIGQUERY_SHUTDOWN_TIMEOUT_SECONDS`, porque os orçamentos
# são de naturezas diferentes: aquele mede uma chamada de rede, este mede o
# escoamento de uma fila local. Somados, cabem com folga nos 60s de
# `terminationGracePeriodSeconds`.
BIGQUERY_SHUTDOWN_DRAIN_SECONDS = float(
    getenv_or_action("BIGQUERY_SHUTDOWN_DRAIN_SECONDS", default="3.0", action="ignore")
)
# Teto de itens por chave da DLQ no Redis. A DLQ divide instância com o cache
# de queries: sem teto, uma indisponibilidade longa do BigQuery encheria a
# memória do Redis e derrubaria o cache junto — falha de escrita virando
# degradação de leitura.
BIGQUERY_DLQ_MAX_ITEMS = int(
    getenv_or_action("BIGQUERY_DLQ_MAX_ITEMS", default="1000", action="ignore")
)
# Validade das chaves da DLQ. Além de evitar chave órfã, é o que limita a
# retenção do dado pessoal que vai no payload (user_id é telefone; alerta do
# COR carrega endereço e coordenada). O relógio conta a partir da última
# gravação na chave, não por item.
BIGQUERY_DLQ_TTL_SECONDS = int(
    getenv_or_action("BIGQUERY_DLQ_TTL_SECONDS", default="604800", action="ignore")
)
# Intervalo entre varreduras do worker que devolve a DLQ ao BigQuery. Folgado
# porque o cenário que enche a DLQ é indisponibilidade do BigQuery: reprocessar
# de minuto em minuto só empilharia falha sobre falha enquanto ele não volta.
BIGQUERY_DLQ_DRAIN_INTERVAL_SECONDS = float(
    getenv_or_action(
        "BIGQUERY_DLQ_DRAIN_INTERVAL_SECONDS", default="300", action="ignore"
    )
)
# Quantos itens de DLQ o worker reprocessa por varredura. Limita o tamanho do
# lote que ocupa uma thread de escrita de cada vez, para o drain não competir
# com a gravação corrente.
BIGQUERY_DLQ_DRAIN_BATCH = int(
    getenv_or_action("BIGQUERY_DLQ_DRAIN_BATCH", default="100", action="ignore")
)
# Liga/desliga o worker automático de drain. Desligar deixa a DLQ apenas sob
# reprocessamento manual (`python -m src.utils.bq_dlq_replay`).
BIGQUERY_DLQ_DRAIN_ENABLED = getenv_bool("BIGQUERY_DLQ_DRAIN_ENABLED", default="true")
# Prazo que a tool de alerta do COR espera pelo registro no BigQuery antes de
# responder. Alerta de severidade alta/crítica não passa pelo lote (ver
# `_SEVERIDADES_SEM_LOTE`), então é insert direto com retry: sem teto, uma
# indisponibilidade do BigQuery deixaria a tool pendurada por dezenas de
# segundos — durante a ocorrência, que é justamente quando a resposta importa.
# Estourado o prazo, a escrita continua em background e a durabilidade fica com
# retry e DLQ; o que se abre mão é só da confirmação síncrona.
COR_ALERT_WRITE_DEADLINE_SECONDS = float(
    getenv_or_action("COR_ALERT_WRITE_DEADLINE_SECONDS", default="8.0", action="ignore")
)

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

OTEL_SERVICE_NAME = getenv_or_action(
    "OTEL_SERVICE_NAME", default="app-mcp-server", action="ignore"
)
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT = getenv_or_action(
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", action="ignore"
)
OTEL_EXPORTER_OTLP_TRACES_HEADERS = getenv_or_action(
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS", action="ignore"
)

# Short-term memory limits (kept as strings for deployment)
SHORT_MEMORY_TIME_LIMIT = getenv_or_action(
    "SHORT_MEMORY_TIME_LIMIT", default="30"
)  # in days
SHORT_MEMORY_TOKEN_LIMIT = getenv_or_action(
    "SHORT_MEMORY_TOKEN_LIMIT", default="50000"
)  # in tokens

# SGRC Configuration
SGRC_URL = getenv_or_action("SGRC_URL")
SGRC_AUTHORIZATION_HEADER = getenv_or_action("SGRC_AUTHORIZATION_HEADER")
SGRC_BODY_TOKEN = getenv_or_action("SGRC_BODY_TOKEN")
GMAPS_API_TOKEN = getenv_or_action("GMAPS_API_TOKEN")
# `Path` e não `str`: os consumidores fazem `env.DATA_DIR / "arquivo.json"`
# (ver workflows/poda_de_arvore/api/api_service.py).
DATA_DIR = Path(getenv_or_action("DATA_DIR"))

TYPESENSE_ACTIVE = getenv_or_action("TYPESENSE_ACTIVE", default="false", action="warn")
TYPESENSE_PARAMETERS = getenv_or_action("TYPESENSE_PARAMETERS")
PODA_SERVICE_ID = getenv_or_action("PODA_SERVICE_ID", action="ignore")
