import asyncio
import functools
import hashlib
from google.cloud import bigquery
from google.api_core.exceptions import NotFound
from google.oauth2 import service_account
from opentelemetry.trace import Status, StatusCode
from typing import List
import base64
import json
import src.config.env as env
from datetime import datetime, date, time
import pytz
from src.observability.tracing import get_tracer
from src.utils.log import logger
from src.utils.error_interceptor import interceptor
from src.utils.json_utils import CustomJSONEncoder


@functools.lru_cache(maxsize=1)
def get_bigquery_client() -> bigquery.Client:
    """Get the BigQuery client.

    The client is constructed once per process and cached for reuse across all
    call sites.  ``lru_cache`` provides thread-safe initialisation in CPython
    (the GIL ensures only one thread executes the body for a given argument
    set), so concurrent callers from ``loop.run_in_executor`` threads will all
    receive the same instance without any additional locking.

    Credential rotation is handled by the Argo Rollouts / Infisical
    ``auto-reload`` annotation, which triggers a full pod restart on secret
    change — so the cached client is always constructed with the latest
    credentials at startup.

    Returns:
        bigquery.Client: The BigQuery client (singleton for the process lifetime)
    """
    credentials = get_gcp_credentials(
        scopes=[
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/cloud-platform",
        ]
    )
    return bigquery.Client(credentials=credentials, project=credentials.project_id)


def get_gcp_credentials(scopes: List[str] = None) -> service_account.Credentials:
    """Get the GCP credentials.

    Args:
        scopes (List[str], optional): The scopes to use. Defaults to None.

    Returns:
        service_account.Credentials: The GCP credentials.
    """
    info: dict = json.loads(base64.b64decode(env.GCP_SERVICE_ACCOUNT_CREDENTIALS))
    creds = service_account.Credentials.from_service_account_info(info)
    if scopes:
        creds = creds.with_scopes(scopes)
    return creds


def get_datetime() -> str:
    timestamp = datetime.now(pytz.timezone("America/Sao_Paulo"))
    return timestamp.strftime("%Y-%m-%dT%H:%M:%S.%f")


@interceptor(source={"source": "mcp", "tool": "bigquery"})
def save_response_in_bq(
    data: dict,
    endpoint: str,
    dataset_id: str,
    table_id: str,
    project_id: str = "rj-iplanrio",
    environment: str = None,
):
    from src.config.env import ENVIRONMENT

    # Use passed environment or default from config
    env_value = environment if environment is not None else ENVIRONMENT

    table_full_name = f"{project_id}.{dataset_id}.{table_id}"
    logger.info(f"Salvando resposta no BigQuery: {table_full_name}")
    datetime_to_save = get_datetime()
    data_to_save = {
        "datetime": datetime_to_save,
        "endpoint": endpoint,
        "data": json.dumps(data, cls=CustomJSONEncoder),
        "environment": env_value,
        "data_particao": datetime_to_save.split("T")[0],
    }
    json_data = [data_to_save]
    client = get_bigquery_client()

    tracer = get_tracer()
    with tracer.start_as_current_span("bigquery.save_response") as span:
        span.set_attribute("bigquery.project_id", project_id)
        span.set_attribute("bigquery.dataset_id", dataset_id)
        span.set_attribute("bigquery.table_id", table_id)
        span.set_attribute("bigquery.endpoint", endpoint)
        span.set_attribute("bigquery.row_count", len(json_data))
        try:
            errors = client.insert_rows_json(table_full_name, json_data)
            if errors:
                error_msgs = [
                    f"Row {e.get('index', '?')}: {e.get('errors', e)}" for e in errors
                ]
                raise Exception(f"Erro ao inserir no BigQuery: {'; '.join(error_msgs)}")
            span.set_attribute("bigquery.success", True)
            span.set_status(Status(StatusCode.OK))
        except Exception as e:
            span.set_attribute("bigquery.success", False)
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            logger.error(f"Erro ao salvar resposta no BigQuery: {str(e)}")
            raise


async def save_response_in_bq_background(
    data, endpoint, dataset_id, table_id, environment=None
):
    """
    Asynchronous wrapper for saving the response in BigQuery.
    Catches and logs exceptions to prevent crashing background tasks.
    """
    try:
        # Since save_response_in_bq is a regular synchronous function,
        # we run it in an executor to avoid blocking the event loop.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,  # Uses the default ThreadPoolExecutor
            save_response_in_bq,
            data,
            endpoint,
            dataset_id,
            table_id,
            "rj-iplanrio",  # project_id
            environment,
        )
    except Exception:
        logger.exception(
            f"Failed to save response to BigQuery in background for endpoint: {endpoint}"
        )


@interceptor(
    source={"source": "mcp", "tool": "bigquery"},
    extract_user_id=lambda args, kwargs: (
        kwargs.get("user_id") or (args[0] if args else "unknown")
    ),
)
def save_feedback_in_bq(
    user_id: str,
    feedback: str,
    timestamp: str,
    environment: str,
    dataset_id: str = "brutos_eai_logs",
    table_id: str = "feedback",
    project_id: str = "rj-iplanrio",
):
    """
    Saves user feedback directly to BigQuery with feedback-specific schema.

    Args:
        user_id: User identifier
        feedback: User feedback text
        timestamp: Timestamp when feedback was submitted
        environment: Environment where feedback was generated (staging, prod, etc.)
        dataset_id: BigQuery dataset ID
        table_id: BigQuery table ID
        project_id: GCP project ID
    """
    table_full_name = f"{project_id}.{dataset_id}.{table_id}"
    logger.info(f"Salvando feedback no BigQuery: {table_full_name}")

    data_to_save = {
        "user_id": user_id,
        "feedback": feedback,
        "environment": environment,
        "timestamp": timestamp,
        "data_particao": timestamp.split("T")[0],
    }

    json_data = json.loads(json.dumps([data_to_save]))
    client = get_bigquery_client()

    tracer = get_tracer()
    with tracer.start_as_current_span("bigquery.save_feedback") as span:
        span.set_attribute("bigquery.project_id", project_id)
        span.set_attribute("bigquery.dataset_id", dataset_id)
        span.set_attribute("bigquery.table_id", table_id)
        span.set_attribute("bigquery.row_count", len(json_data))
        try:
            errors = client.insert_rows_json(table_full_name, json_data)
            if errors:
                error_msgs = [
                    f"Row {e.get('index', '?')}: {e.get('errors', e)}" for e in errors
                ]
                raise Exception(
                    f"Erro ao inserir feedback no BigQuery: {'; '.join(error_msgs)}"
                )
            logger.info(f"Feedback salvo no BigQuery: {table_full_name}")
            span.set_attribute("bigquery.success", True)
            span.set_status(Status(StatusCode.OK))
        except Exception as e:
            span.set_attribute("bigquery.success", False)
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            logger.error(f"Erro ao salvar feedback no BigQuery: {str(e)}")
            raise


async def save_feedback_in_bq_background(
    user_id: str,
    feedback: str,
    timestamp: str,
    environment: str,
    dataset_id: str = "brutos_eai_logs",
    table_id: str = "feedback",
):
    """
    Asynchronous wrapper for saving feedback in BigQuery.
    Catches and logs exceptions to prevent crashing background tasks.
    """
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            save_feedback_in_bq,
            user_id,
            feedback,
            timestamp,
            environment,
            dataset_id,
            table_id,
        )
    except Exception:
        logger.exception(
            f"Failed to save feedback to BigQuery in background for user: {user_id}"
        )


@interceptor(
    source={"source": "mcp", "tool": "bigquery"},
    extract_user_id=lambda args, kwargs: (
        kwargs.get("user_id") or (args[1] if len(args) > 1 else "unknown")
    ),
)
def save_cor_alert_in_bq(
    alert_id: str,
    user_id: str,
    alert_type: str,
    severity: str,
    description: str,
    address: str,
    latitude: float,
    longitude: float,
    timestamp: str,
    environment: str,
    dataset_id: str = "brutos_eai_logs",
    table_id: str = "cor_alerts",
    project_id: str = "rj-iplanrio",
):
    """
    Saves COR alert directly to BigQuery with alert-specific schema.

    Args:
        alert_id: Unique alert identifier (UUID)
        user_id: User identifier
        alert_type: Type of alert ("alagamento", "enchente", "dano_chuva")
        severity: Alert severity ("alta" or "critica")
        description: Detailed description of the problem
        address: Address provided by user
        latitude: Geocoded latitude (nullable)
        longitude: Geocoded longitude (nullable)
        timestamp: Timestamp when alert was created
        environment: Environment where alert was generated (staging, prod, etc.)
        dataset_id: BigQuery dataset ID
        table_id: BigQuery table ID
        project_id: GCP project ID
    """
    table_full_name = f"{project_id}.{dataset_id}.{table_id}"
    logger.info(f"Salvando alerta COR no BigQuery: {table_full_name}")

    data_to_save = {
        "alert_id": alert_id,
        "user_id": user_id,
        "alert_type": alert_type,
        "severity": severity,
        "description": description,
        "address": address,
        "latitude": latitude,
        "longitude": longitude,
        "created_at": timestamp,
        "environment": environment,
        "data_particao": timestamp.split("T")[0],
    }

    json_data = json.loads(json.dumps([data_to_save]))
    client = get_bigquery_client()

    tracer = get_tracer()
    with tracer.start_as_current_span("bigquery.save_cor_alert") as span:
        span.set_attribute("bigquery.project_id", project_id)
        span.set_attribute("bigquery.dataset_id", dataset_id)
        span.set_attribute("bigquery.table_id", table_id)
        span.set_attribute("bigquery.alert_type", alert_type)
        span.set_attribute("bigquery.severity", severity)
        span.set_attribute("bigquery.row_count", len(json_data))
        try:
            errors = client.insert_rows_json(table_full_name, json_data)
            if errors:
                error_msgs = [
                    f"Row {e.get('index', '?')}: {e.get('errors', e)}" for e in errors
                ]
                raise Exception(
                    f"Erro ao inserir alerta COR no BigQuery: {'; '.join(error_msgs)}"
                )
            logger.info(f"Alerta COR salvo no BigQuery: {table_full_name}")
            span.set_attribute("bigquery.success", True)
            span.set_status(Status(StatusCode.OK))
        except Exception as e:
            span.set_attribute("bigquery.success", False)
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            logger.error(f"Erro ao salvar alerta COR no BigQuery: {str(e)}")
            raise


async def save_cor_alert_in_bq_background(
    alert_id: str,
    user_id: str,
    alert_type: str,
    severity: str,
    description: str,
    address: str,
    latitude: float,
    longitude: float,
    timestamp: str,
    environment: str,
    bairro_raw: str = None,
    bairro_normalizado: str = None,
    dataset_id: str = "brutos_eai_logs",
    table_id: str = "cor_alerts",
):
    """
    Asynchronous wrapper for saving COR alert in BigQuery.
    Catches and logs exceptions to prevent crashing background tasks.
    """

    def _normalize(value: str) -> str:
        if not value:
            return ""
        import unicodedata

        normalized = unicodedata.normalize("NFKD", value.strip())
        without_accents = "".join(
            char for char in normalized if not unicodedata.combining(char)
        )
        return " ".join(without_accents.lower().split())

    def _infer_neighborhood_from_address(raw_address: str) -> str:
        if not raw_address:
            return ""
        address_norm = _normalize(raw_address)
        if "jardim america" in address_norm or "jd america" in address_norm:
            return "jardim america"
        if "acari" in address_norm:
            return "acari"
        if "guaratiba" in address_norm:
            return "guaratiba"
        return ""

    raw_candidate = (bairro_raw or "").strip()
    normalized_candidate = _normalize(bairro_normalizado or "")
    inferred_candidate = _infer_neighborhood_from_address(address)

    final_bairro_raw = raw_candidate or inferred_candidate
    final_bairro_normalizado = (
        normalized_candidate or _normalize(final_bairro_raw) or inferred_candidate
    )

    if final_bairro_normalizado == "jd america":
        final_bairro_normalizado = "jardim america"

    def _save_alert_with_neighborhood():
        table_full_name = f"rj-iplanrio.{dataset_id}.{table_id}"
        payload = [
            {
                "alert_id": alert_id,
                "user_id": user_id,
                "alert_type": alert_type,
                "severity": severity,
                "description": description,
                "address": address,
                "latitude": latitude,
                "longitude": longitude,
                "bairro_raw": final_bairro_raw or None,
                "bairro_normalizado": final_bairro_normalizado or None,
                "created_at": timestamp,
                "environment": environment,
                "data_particao": timestamp.split("T")[0],
            }
        ]
        client = get_bigquery_client()
        errors = client.insert_rows_json(table_full_name, payload)
        if errors:
            error_msgs = [
                f"Row {e.get('index', '?')}: {e.get('errors', e)}" for e in errors
            ]
            raise Exception(
                f"Erro ao inserir alerta COR no BigQuery: {'; '.join(error_msgs)}"
            )
        logger.info(f"Alerta COR salvo no BigQuery: {table_full_name}")

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _save_alert_with_neighborhood)
    except Exception:
        logger.exception(
            f"Failed to save COR alert to BigQuery in background for alert_id: {alert_id}"
        )


def save_cor_alert_to_queue(
    alert_id: str,
    user_id: str,
    alert_type: str,
    severity: str,
    description: str,
    address: str,
    latitude: float,
    longitude: float,
    timestamp: str,
    environment: str,
    bairro_raw: str = None,
    bairro_normalizado: str = None,
    dataset_id: str = "brutos_eai_logs",
    table_id: str = "cor_alerts_queue",
    project_id: str = "rj-iplanrio",
):
    """
    Saves COR alert to queue table for aggregation processing by Prefect pipeline.

    The alert is saved with status='pending' and will be processed by the
    rj_iplanrio__cor_alerts_aggregator pipeline which runs every 2 minutes.

    Aggregation rules:
    - Alerts are grouped by type (enchente, alagamento, bolsao) within 500m radius
    - 5+ alerts in cluster: dispatch immediately
    - 1-4 alerts + 7 min window expired: dispatch
    - 1-4 alerts + window active: wait for more

    Args:
        alert_id: Unique alert identifier (UUID)
        user_id: User identifier
        alert_type: Type of alert ("alagamento", "enchente", "bolsao")
        severity: Alert severity ("alta" or "critica")
        description: Detailed description of the problem
        address: Address provided by user
        latitude: Geocoded latitude (nullable)
        longitude: Geocoded longitude (nullable)
        timestamp: Timestamp when alert was created
        environment: Environment (staging, prod)
        dataset_id: BigQuery dataset ID
        table_id: BigQuery table ID
        project_id: GCP project ID
    """
    table_full_name = f"{project_id}.{dataset_id}.{table_id}"
    logger.info(f"Salvando alerta COR na fila: {table_full_name}")

    data_to_save = {
        "alert_id": alert_id,
        "user_id": user_id,
        "alert_type": alert_type,
        "severity": severity,
        "description": description,
        "address": address,
        "latitude": latitude,
        "longitude": longitude,
        "bairro_raw": bairro_raw,
        "bairro_normalizado": bairro_normalizado,
        "created_at": timestamp,
        "status": "pending",
        "aggregation_group_id": None,
        "sent_at": None,
        "environment": environment,
        "data_particao": timestamp.split("T")[0],
    }

    json_data = json.loads(json.dumps([data_to_save]))
    client = get_bigquery_client()

    try:
        errors = client.insert_rows_json(table_full_name, json_data)
        if errors:
            error_msgs = [
                f"Row {e.get('index', '?')}: {e.get('errors', e)}" for e in errors
            ]
            raise Exception(
                f"Erro ao inserir alerta COR na fila: {'; '.join(error_msgs)}"
            )
        logger.info(f"Alerta COR salvo na fila: {alert_id}")
    except Exception as e:
        logger.error(f"Erro ao salvar alerta COR na fila: {str(e)}")
        raise


async def save_cor_alert_to_queue_background(
    alert_id: str,
    user_id: str,
    alert_type: str,
    severity: str,
    description: str,
    address: str,
    latitude: float,
    longitude: float,
    timestamp: str,
    environment: str,
    bairro_raw: str = None,
    bairro_normalizado: str = None,
    dataset_id: str = "brutos_eai_logs",
    table_id: str = "cor_alerts_queue",
):
    """
    Asynchronous wrapper for saving COR alert to queue in BigQuery.
    Catches and logs exceptions to prevent crashing background tasks.
    """
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            save_cor_alert_to_queue,
            alert_id,
            user_id,
            alert_type,
            severity,
            description,
            address,
            latitude,
            longitude,
            timestamp,
            environment,
            bairro_raw,
            bairro_normalizado,
            dataset_id,
            table_id,
        )
    except Exception:
        logger.exception(
            f"Failed to save COR alert to queue in background for alert_id: {alert_id}"
        )


_async_redis_client = None


async def get_async_redis_client():
    """Get or create process-wide async Redis client instance."""
    global _async_redis_client
    if _async_redis_client is None:
        try:
            import redis.asyncio as redis

            redis_url = getattr(env, "REDIS_URL", None)

            if redis_url:
                _async_redis_client = redis.Redis.from_url(
                    redis_url, decode_responses=True
                )
        except Exception as e:
            logger.warning(f"Could not initialize async Redis client: {e}")
            _async_redis_client = None
    return _async_redis_client


def _generate_cache_key(query: str, query_parameters: list = None) -> str:
    params_str = ""
    if query_parameters:
        parts = []
        for p in query_parameters:
            name = getattr(p, "name", "")
            type_ = getattr(p, "type_", getattr(p, "array_type", ""))
            val = getattr(p, "value", getattr(p, "values", str(p)))
            parts.append(f"{name}:{type_}:{val}")
        params_str = "|".join(parts)
    raw_key = f"{query}:{params_str}"
    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    return f"bq_cache:{key_hash}"


def _execute_bigquery_query(
    query: str,
    query_parameters: list = None,
    page_size: int = None,
    timeout_seconds: float = None,
) -> List[dict]:
    """Synchronous execution of BigQuery query, designed to run in thread executor."""
    default_page_size = getattr(env, "GOOGLE_BIGQUERY_PAGE_SIZE", 100)
    default_timeout = getattr(env, "BIGQUERY_TIMEOUT_SECONDS", 10.0)

    page_size = page_size if page_size is not None else default_page_size
    timeout_seconds = (
        timeout_seconds if timeout_seconds is not None else default_timeout
    )
    client = get_bigquery_client()

    tracer = get_tracer()
    with tracer.start_as_current_span("bigquery.query") as span:
        span.set_attribute("bigquery.page_size", page_size)
        span.set_attribute("bigquery.query_length", len(query))
        try:
            logger.info(f"Executando query no BigQuery: {query[:100]}...")
            job_config = None
            if query_parameters:
                job_config = bigquery.QueryJobConfig(query_parameters=query_parameters)

            if job_config is not None:
                query_job = client.query(query, job_config=job_config)
            else:
                query_job = client.query(query)

            results = query_job.result(page_size=page_size, timeout=timeout_seconds)

            # Convert results to list of dictionaries
            rows = []
            for row in results:
                row_dict = {}
                for key, value in row.items():
                    if isinstance(value, (datetime, date, time)):
                        row_dict[key] = value.isoformat()
                    else:
                        row_dict[key] = value
                rows.append(row_dict)

            logger.info(f"Query executada com sucesso. {len(rows)} linhas retornadas.")
            span.set_attribute("bigquery.row_count", len(rows))
            span.set_attribute("bigquery.success", True)
            span.set_status(Status(StatusCode.OK))
            return rows
        except NotFound as e:
            span.set_attribute("bigquery.row_count", 0)
            span.set_attribute("bigquery.table_not_found", True)
            span.set_attribute("bigquery.success", True)
            span.set_status(Status(StatusCode.OK))
            logger.warning(f"Tabela não encontrada no BigQuery: {str(e)}")
            # Return empty list when table doesn't exist yet - allows graceful degradation
            return []
        except Exception as e:
            span.set_attribute("bigquery.success", False)
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            logger.error(f"Erro ao executar query no BigQuery: {str(e)}")
            raise Exception(f"Failed to execute BigQuery query: {str(e)}")


@interceptor(source={"source": "mcp", "tool": "bigquery"})
async def get_bigquery_result(
    query: str,
    query_parameters: list = None,
    page_size: int = None,
    cache_ttl_seconds: int = None,
    timeout_seconds: float = None,
) -> List[dict]:
    """
    Executes a BigQuery query with caching and non-blocking executor execution.

    Args:
        query: SQL query to execute
        query_parameters: List of BigQuery QueryParameter objects
        page_size: Number of rows per page (optional, uses env default)
        cache_ttl_seconds: Cache TTL in seconds (0 to bypass cache)
        timeout_seconds: Query execution timeout in seconds

    Returns:
        List of dictionaries with query results
    """
    default_ttl = getattr(env, "BIGQUERY_CACHE_TTL_SECONDS", 3600)
    default_timeout = getattr(env, "BIGQUERY_TIMEOUT_SECONDS", 10.0)

    ttl = cache_ttl_seconds if cache_ttl_seconds is not None else default_ttl
    timeout = timeout_seconds if timeout_seconds is not None else default_timeout

    cache_key = None
    if ttl > 0:
        cache_key = _generate_cache_key(query, query_parameters)
        try:
            redis_client = await get_async_redis_client()
            if redis_client is not None:
                cached_data = await redis_client.get(cache_key)
                if cached_data is not None:
                    logger.info(f"BigQuery cache hit para chave: {cache_key}")
                    if isinstance(cached_data, bytes):
                        cached_data = cached_data.decode("utf-8")
                    return json.loads(cached_data)
        except Exception as e:
            logger.warning(f"Erro ao ler cache do Redis para BigQuery: {e}")

    loop = asyncio.get_running_loop()
    try:
        rows = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                _execute_bigquery_query,
                query,
                query_parameters,
                page_size,
                timeout,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.error(f"Timeout de {timeout}s excedido ao executar query BigQuery")
        raise TimeoutError(f"BigQuery query execution timed out after {timeout}s")

    if ttl > 0 and cache_key is not None:
        try:
            redis_client = await get_async_redis_client()
            if redis_client is not None:
                serialized = json.dumps(rows, cls=CustomJSONEncoder)
                await redis_client.setex(cache_key, ttl, serialized)
        except Exception as e:
            logger.warning(f"Erro ao gravar cache no Redis para BigQuery: {e}")

    return rows
