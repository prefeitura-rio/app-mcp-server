import asyncio
import atexit
import contextlib
import functools
import hashlib
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from google.cloud import bigquery
from google.api_core.exceptions import GoogleAPIError, NotFound
from google.oauth2 import service_account
from opentelemetry.trace import Status, StatusCode
from typing import List, NamedTuple
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


_batch_buffer_lock = threading.Lock()
_batch_buffer: dict = {}

# ---------------------------------------------------------------------------
# Background flush thread — drains the batch buffer periodically so rows are
# not stranded in memory when volume is too low to hit the batch_size threshold.
# _start_batch_flush_thread() is called at the bottom of this module, after
# flush_bigquery_batch_buffer is defined.
# ---------------------------------------------------------------------------

_flush_thread: threading.Thread | None = None
_flush_stop_event = threading.Event()

_FLUSH_INTERVAL_SECONDS = 30  # override via env at start-up if needed


def _flush_loop() -> None:
    """Run in a daemon thread; flushes all pending rows every interval."""
    while not _flush_stop_event.wait(timeout=_FLUSH_INTERVAL_SECONDS):
        try:
            flush_bigquery_batch_buffer()
        except Exception:
            pass  # errors already logged inside flush_bigquery_batch_buffer


def _start_batch_flush_thread() -> None:
    """Start the periodic flush daemon thread (idempotent)."""
    global _flush_thread
    if _flush_thread is not None and _flush_thread.is_alive():
        return
    _flush_stop_event.clear()
    _flush_thread = threading.Thread(
        target=_flush_loop, name="bq-batch-flusher", daemon=True
    )
    _flush_thread.start()


def _stop_batch_flush_thread() -> None:
    """Signal the flush thread to stop and do a final flush (called on shutdown)."""
    _flush_stop_event.set()
    try:
        flush_bigquery_batch_buffer()
    except Exception:
        pass


_sync_redis_client = None
_sync_redis_lock = threading.Lock()


def _get_sync_redis_client():
    """Return a process-wide synchronous Redis client, or None if unavailable.

    Os timeouts de socket são obrigatórios pelo mesmo motivo do cliente async
    do cache (ver ``get_async_redis_client``), mas com uma consequência pior:
    este cliente roda em thread do executor default, chamado por
    ``_persist_to_dlq`` no caminho de falha de escrita. Sem timeout, um Redis
    que aceita a conexão e para de responder prende a thread indefinidamente —
    e, como a chamada nunca retorna, o fallback em arquivo logo abaixo (que
    existe justamente para quando o Redis não está disponível) nunca chega a
    ser tentado. O resultado é o pior possível: thread perdida *e* registro
    perdido.
    """
    global _sync_redis_client
    if _sync_redis_client is not None:
        return _sync_redis_client
    with _sync_redis_lock:
        if _sync_redis_client is not None:
            return _sync_redis_client
        try:
            import redis as _redis_lib

            redis_url = getattr(env, "REDIS_URL", None)
            if redis_url:
                timeout = getattr(env, "REDIS_DLQ_TIMEOUT_SECONDS", 5.0)
                _sync_redis_client = _redis_lib.Redis.from_url(
                    redis_url,
                    decode_responses=True,
                    socket_connect_timeout=timeout,
                    socket_timeout=timeout,
                )
        except Exception as e:
            logger.warning(f"Could not initialize sync Redis client: {e}")
    return _sync_redis_client


def _persist_to_dlq(
    table_full_name: str, json_data: List[dict], error_msg: str
) -> None:
    """
    Persists failed payload to Redis Dead-Letter Queue (DLQ) or fallback file storage.

    Priority:
    1. Redis (reuses process-wide singleton client)
    2. Local .jsonl file under DATA_DIR/bq_dlq/ (always has a safe default path)
    """
    from pathlib import Path

    dlq_item = {
        "table_full_name": table_full_name,
        "failed_at": get_datetime(),
        "error": error_msg,
        "payload": json_data,
    }
    serialized = json.dumps(dlq_item, cls=CustomJSONEncoder)

    pushed = False
    try:
        r = _get_sync_redis_client()
        if r is not None:
            r.rpush(f"bq_dlq:{table_full_name}", serialized)
            pushed = True
            logger.error(
                f"Falha definitiva de escrita no BigQuery ({table_full_name}). "
                f"{len(json_data)} registro(s) salvos na DLQ Redis (chave: bq_dlq:{table_full_name}). Erro: {error_msg}"
            )
    except Exception as redis_err:
        logger.warning(f"Não foi possível salvar na DLQ do Redis: {redis_err}")

    if not pushed:
        try:
            data_dir_path = getattr(env, "DATA_DIR", None) or "scratch"
            dlq_dir = Path(data_dir_path) / "bq_dlq"
            dlq_dir.mkdir(parents=True, exist_ok=True)
            safe_table_name = table_full_name.replace(".", "_")
            dlq_file = dlq_dir / f"dlq_{safe_table_name}.jsonl"
            with open(dlq_file, "a", encoding="utf-8") as f:
                f.write(serialized + "\n")
            logger.error(
                f"Falha definitiva de escrita no BigQuery ({table_full_name}). "
                f"{len(json_data)} registro(s) salvos na DLQ em arquivo ({dlq_file}). Erro: {error_msg}"
            )
        except Exception as file_err:
            logger.error(f"CRÍTICO: Falha ao salvar DLQ em arquivo: {file_err}")


def insert_rows_json_with_retry_and_dlq(
    table_full_name: str,
    json_data: List[dict],
    max_retries: int = 3,
    initial_delay: float = 0.5,
) -> None:
    """
    Inserts rows into BigQuery with exponential backoff retries and Dead-Letter Queue (DLQ) fallback.
    """
    client = get_bigquery_client()
    last_exception = None

    for attempt in range(1, max_retries + 1):
        try:
            errors = client.insert_rows_json(table_full_name, json_data)
            if not errors:
                return
            error_msgs = [
                f"Row {e.get('index', '?')}: {e.get('errors', e)}" for e in errors
            ]
            raise Exception(f"Erro ao inserir no BigQuery: {'; '.join(error_msgs)}")
        except Exception as e:
            last_exception = e
            logger.warning(
                f"Tentativa {attempt}/{max_retries} de inserção no BigQuery falhou para {table_full_name}: {e}"
            )
            if attempt < max_retries:
                import time

                time.sleep(initial_delay * (2 ** (attempt - 1)))

    _persist_to_dlq(table_full_name, json_data, str(last_exception))
    raise last_exception


def enqueue_bigquery_row(
    table_full_name: str, row: dict, batch_size: int = None
) -> None:
    """
    Enqueues a row to the BigQuery batch buffer.
    Flushes automatically when pending rows reach batch_size threshold.
    """
    batch_size = (
        batch_size
        if batch_size is not None
        else getattr(env, "BIGQUERY_BATCH_SIZE", 50)
    )

    rows_to_flush = None
    with _batch_buffer_lock:
        if table_full_name not in _batch_buffer:
            _batch_buffer[table_full_name] = []
        _batch_buffer[table_full_name].append(row)

        if len(_batch_buffer[table_full_name]) >= batch_size:
            rows_to_flush = _batch_buffer[table_full_name]
            _batch_buffer[table_full_name] = []

    if rows_to_flush:
        insert_rows_json_with_retry_and_dlq(table_full_name, rows_to_flush)


def flush_bigquery_batch_buffer(table_full_name: str = None) -> None:
    """
    Flushes pending rows in the batch buffer to BigQuery.
    If table_full_name is specified, flushes only that table. Otherwise flushes all tables.
    """
    tables_to_flush = {}
    with _batch_buffer_lock:
        if table_full_name:
            if table_full_name in _batch_buffer and _batch_buffer[table_full_name]:
                tables_to_flush[table_full_name] = _batch_buffer[table_full_name]
                _batch_buffer[table_full_name] = []
        else:
            for tbl, rows in _batch_buffer.items():
                if rows:
                    tables_to_flush[tbl] = rows
            _batch_buffer.clear()

    for tbl, rows in tables_to_flush.items():
        try:
            insert_rows_json_with_retry_and_dlq(tbl, rows)
        except Exception as e:
            logger.error(f"Erro ao descarregar buffer em lote para {tbl}: {e}")


@interceptor(source={"source": "mcp", "tool": "bigquery"})
def save_response_in_bq(
    data: dict,
    endpoint: str,
    dataset_id: str,
    table_id: str,
    project_id: str = "rj-iplanrio",
    environment: str = None,
    use_batch: bool = False,
):
    from src.config.env import ENVIRONMENT

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

    tracer = get_tracer()
    with tracer.start_as_current_span("bigquery.save_response") as span:
        span.set_attribute("bigquery.project_id", project_id)
        span.set_attribute("bigquery.dataset_id", dataset_id)
        span.set_attribute("bigquery.table_id", table_id)
        span.set_attribute("bigquery.endpoint", endpoint)
        span.set_attribute("bigquery.row_count", 1)
        try:
            if use_batch:
                enqueue_bigquery_row(table_full_name, data_to_save)
            else:
                insert_rows_json_with_retry_and_dlq(table_full_name, [data_to_save])
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
    Asynchronous wrapper for saving the response in BigQuery using batching buffer.
    Catches and logs exceptions to prevent crashing background tasks.
    """
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            save_response_in_bq,
            data,
            endpoint,
            dataset_id,
            table_id,
            "rj-iplanrio",
            environment,
            True,  # use_batch=True
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
    use_batch: bool = False,
):
    table_full_name = f"{project_id}.{dataset_id}.{table_id}"
    logger.info(f"Salvando feedback no BigQuery: {table_full_name}")

    data_to_save = {
        "user_id": user_id,
        "feedback": feedback,
        "environment": environment,
        "timestamp": timestamp,
        "data_particao": timestamp.split("T")[0],
    }

    tracer = get_tracer()
    with tracer.start_as_current_span("bigquery.save_feedback") as span:
        span.set_attribute("bigquery.project_id", project_id)
        span.set_attribute("bigquery.dataset_id", dataset_id)
        span.set_attribute("bigquery.table_id", table_id)
        span.set_attribute("bigquery.row_count", 1)
        try:
            if use_batch:
                enqueue_bigquery_row(table_full_name, data_to_save)
            else:
                insert_rows_json_with_retry_and_dlq(table_full_name, [data_to_save])
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
    Asynchronous wrapper for saving feedback in BigQuery using batching buffer.
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
            "rj-iplanrio",
            True,  # use_batch=True
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
    bairro_raw: str = None,
    bairro_normalizado: str = None,
    dataset_id: str = "brutos_eai_logs",
    table_id: str = "cor_alerts",
    project_id: str = "rj-iplanrio",
    use_batch: bool = False,
):
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
        "bairro_raw": bairro_raw,
        "bairro_normalizado": bairro_normalizado,
        "created_at": timestamp,
        "environment": environment,
        "data_particao": timestamp.split("T")[0],
    }

    tracer = get_tracer()
    with tracer.start_as_current_span("bigquery.save_cor_alert") as span:
        span.set_attribute("bigquery.project_id", project_id)
        span.set_attribute("bigquery.dataset_id", dataset_id)
        span.set_attribute("bigquery.table_id", table_id)
        span.set_attribute("bigquery.alert_type", alert_type)
        span.set_attribute("bigquery.severity", severity)
        span.set_attribute("bigquery.row_count", 1)
        try:
            if use_batch:
                enqueue_bigquery_row(table_full_name, data_to_save)
            else:
                insert_rows_json_with_retry_and_dlq(table_full_name, [data_to_save])
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

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            save_cor_alert_in_bq,
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
            final_bairro_raw or None,
            final_bairro_normalizado or None,
            dataset_id,
            table_id,
            "rj-iplanrio",
            True,  # use_batch=True
        )
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
    use_batch: bool = False,
):
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

    try:
        if use_batch:
            enqueue_bigquery_row(table_full_name, data_to_save)
        else:
            insert_rows_json_with_retry_and_dlq(table_full_name, [data_to_save])
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
            "rj-iplanrio",
            False,  # use_batch=False — entrega imediata para o pipeline Prefect
        )
    except Exception:
        logger.exception(
            f"Failed to save COR alert to queue in background for alert_id: {alert_id}"
        )


_async_redis_client = None


async def get_async_redis_client():
    """Get or create process-wide async Redis client instance.

    Os timeouts de socket não são opcionais aqui. Sem eles, um Redis que aceita
    a conexão mas para de responder (partição de rede, não recusa) pendura o
    ``await`` do cache indefinidamente — e a leitura do cache acontece *fora* do
    ``asyncio.wait_for`` que protege a query, então o timeout do BigQuery não
    cobre esse caso. O cache existe para economizar tempo; sem timeout ele vira
    o caminho mais lento possível. Mesmo padrão já usado pelo backend de sessão
    em ``src/tools/multi_step_service/core/state.py``.
    """
    global _async_redis_client
    if _async_redis_client is None:
        try:
            import redis.asyncio as redis

            redis_url = getattr(env, "REDIS_URL", None)

            if redis_url:
                timeout = getattr(env, "REDIS_CACHE_TIMEOUT_SECONDS", 2.0)
                _async_redis_client = redis.Redis.from_url(
                    redis_url,
                    decode_responses=True,
                    socket_connect_timeout=timeout,
                    socket_timeout=timeout,
                )
        except Exception as e:
            logger.warning(f"Could not initialize async Redis client: {e}")
            _async_redis_client = None
    return _async_redis_client


CACHE_KEY_PREFIX = "bq_cache"


# Caracteres que a chave usa como estrutura. Precisam ser escapados dentro dos
# valores, senão um valor consegue se disfarçar de outro. `%` vem primeiro por
# ser o próprio caractere de escape — inverter a ordem tornaria a codificação
# ambígua (`a%20b` literal viraria indistinguível de `a b`).
_ESCAPES_DA_CHAVE = (("%", "%25"), (":", "%3A"), (",", "%2C"), (" ", "%20"))


def _normalize_cache_part(value) -> str:
    """Reduz um valor de chave a texto estável, escapando os separadores.

    Coleções são ordenadas: o chamador só deve passar coleção quando a ordem
    não altera o resultado da query (é o caso de um filtro ``IN UNNEST``, onde
    ``["A","B"]`` e ``["B","A"]`` devolvem o mesmo). Ordenar transforma essas
    duas chamadas em um único acerto de cache em vez de duas entradas.

    O escape é injetivo de propósito. Substituir os separadores por ``_``,
    como antes, fazia valores distintos colapsarem na mesma chave sem qualquer
    sinal: ``"ASSISTENCIA SOCIAL"`` e ``"ASSISTENCIA_SOCIAL"`` viravam a mesma
    entrada, e ``["A,B"]`` virava a mesma que ``["A","B"]``. O desfecho é o
    pior possível para depurar — resposta errada, sem erro e sem log.

    Percent-encoding em vez de hash porque a chave precisa continuar legível e
    varrível por prefixo, que é a alavanca de invalidação por região. `%` não
    tem significado em glob de ``SCAN``, então os padrões não mudam.
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set, frozenset)):
        return ",".join(sorted(_normalize_cache_part(v) for v in value))
    texto = str(value)
    for bruto, escapado in _ESCAPES_DA_CHAVE:
        texto = texto.replace(bruto, escapado)
    return texto


def _sql_fingerprint(query: str) -> str:
    """Hash curto do texto da query, usado como parte fixa da chave semântica.

    Sem ele, a chave é definida só pelo namespace mais os parâmetros
    semânticos — e nada garante que esse par identifique *uma* query. Dois
    problemas concretos, ambos silenciosos:

    * Um segundo chamador que reaproveite o mesmo namespace (o caso de
      `equipments_categories`, que hoje não passa nenhuma parte e portanto tem
      a chave inteira igual ao namespace) passa a dividir uma entrada com o
      primeiro: quem gravar antes decide o que o outro lê, por todo o TTL.
    * Um deploy que altere o SQL continua servindo o formato antigo até o TTL
      expirar, porque a chave não se mexeu.

    Com o fingerprint, cada texto de query tem seu próprio espaço e um deploy
    que muda a query invalida sozinho o que mudou. A varredura por prefixo
    continua funcionando — `SCAN MATCH bq_cache:equipments:*` pega as duas
    gerações —, então a invalidação manual descrita em `_generate_cache_key`
    não é afetada.

    8 hexes (32 bits) bastam: o espaço é o punhado de queries do repositório,
    não entrada adversarial.
    """
    return hashlib.sha256(query.encode("utf-8")).hexdigest()[:8]


def _generate_cache_key(
    query: str,
    query_parameters: list = None,
    cache_namespace: str = None,
    cache_key_parts: dict = None,
) -> str:
    """Monta a chave do cache.

    Com ``cache_namespace``, a chave é semântica e legível — o que permite
    varrer e invalidar por prefixo::

        bq_cache:equipments:cats=EDUCACAO,SAUDE:plus8=849VVQCH

        SCAN MATCH bq_cache:equipments:*plus8=849VVQCH*   -> uma região
        SCAN MATCH bq_cache:equipments:*                  -> a tool inteira

    A chave *não* carrega versão do SQL (decisão registrada no CHATR-115): um
    deploy que altere o texto da query continua servindo o resultado anterior
    até o TTL expirar. Quando isso não for aceitável, o caminho é apagar o
    namespace com o ``SCAN``/``DEL`` acima como passo do deploy.

    Sem ``cache_namespace`` cai no hash de SQL + parâmetros, que mantém os
    chamadores genéricos funcionando (e nunca colide entre queries distintas).
    """
    if cache_namespace:
        partes = [
            f"{nome}={_normalize_cache_part(valor)}"
            for nome, valor in sorted((cache_key_parts or {}).items())
        ]
        partes.append(f"sql={_sql_fingerprint(query)}")
        return ":".join([CACHE_KEY_PREFIX, cache_namespace, *partes])

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
    return f"{CACHE_KEY_PREFIX}:{key_hash}"


# ---------------------------------------------------------------------------
# Single-flight: uma query por chave, por processo.
#
# Sem isso, quando o TTL de uma região popular expira todas as requisições
# concorrentes daquele instante vão juntas ao BigQuery — exatamente o custo que
# o cache deveria estar cortando. O lock é por processo (não distribuído): com
# poucas réplicas o teto de queries duplicadas passa a ser o número de pods, o
# que resolve o problema sem o round trip e os locks órfãos de um lock no Redis.
# ---------------------------------------------------------------------------

_inflight_locks: dict = {}
_inflight_refs: dict = {}


@contextlib.asynccontextmanager
async def _single_flight(key: str, deadline: float = None):
    """Serializa, dentro do processo, as chamadas que disputam a mesma chave.

    `deadline` é um instante de `loop.time()`, não uma duração: é o mesmo
    prazo que limita a query, compartilhado com ela. Sem esse limite a espera
    aqui não tinha teto nenhum — o `asyncio.wait_for` da query fica *dentro*
    do lock, então ele limitava a query e não a fila. Com uma query lenta na
    frente, a enésima chamada da mesma chave esperava n × timeout, e o
    critério de aceite do CHATR-125 ("não trava o tool call indefinidamente")
    não se sustentava justamente sob a concorrência que o cache existe para
    atender: todo mundo pedindo a mesma região ao mesmo tempo.
    """
    loop = asyncio.get_running_loop()
    lock = _inflight_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _inflight_locks[key] = lock
    _inflight_refs[key] = _inflight_refs.get(key, 0) + 1
    adquirido = False
    try:
        if deadline is None:
            await lock.acquire()
        else:
            restante = deadline - loop.time()
            if restante <= 0:
                raise BigQueryTimeoutError(
                    f"BigQuery single-flight wait timed out for key {key}"
                )
            try:
                # `asyncio.timeout` e não `wait_for`: o `wait_for` embrulha o
                # `acquire()` numa Task separada e cancela *essa* Task quando
                # o prazo estoura, então quem decide o que fazer com um lock
                # que acabou de ser entregue é código fora do nosso escopo.
                # Aqui o cancelamento chega direto neste corpo — ou o
                # `acquire()` retorna e a linha seguinte (sem nenhum `await`
                # no meio) marca `adquirido`, ou ele levanta e não seguramos
                # nada. As duas coisas não têm como divergir, que é o que
                # garante que o `finally` sempre libere o que foi adquirido.
                async with asyncio.timeout(restante):
                    await lock.acquire()
            except asyncio.TimeoutError:
                # `from None`: o TimeoutError do prazo não acrescenta nada ao
                # daqui, e encadear os dois só polui o traceback.
                raise BigQueryTimeoutError(
                    f"BigQuery single-flight wait timed out for key {key}"
                ) from None
        adquirido = True
        yield
    finally:
        if adquirido:
            lock.release()
        _inflight_refs[key] = _inflight_refs.get(key, 1) - 1
        if _inflight_refs[key] <= 0:
            _inflight_refs.pop(key, None)
            _inflight_locks.pop(key, None)


def _ttl_com_jitter(ttl: int) -> int:
    """Encurta o TTL em até 10%, de forma aleatória.

    Entradas gravadas na mesma janela expiram juntas e recriam o pico que o
    single-flight acabou de evitar — só que uma hora depois. O jitter é sempre
    para baixo para que o TTL configurado continue sendo um teto de quão velho
    um dado pode ser servido.
    """
    if ttl <= 1:
        return ttl
    return max(1, int(ttl * random.uniform(0.9, 1.0)))


def _normalize_bq_value(value):
    """Converte um valor do BigQuery para algo que sobrevive a ida e volta em JSON.

    A conversão é *recursiva* de propósito. Um resultado que passa pelo cache é
    serializado e desserializado; um que não passa volta como o objeto Python
    cru. Se a normalização parasse no primeiro nível, um ``TIMESTAMP`` dentro de
    um ``STRUCT`` voltaria como `datetime` em cache miss e como string ISO em
    cache hit — o mesmo tool call devolvendo tipos diferentes conforme o estado
    do cache, que é a classe de bug mais difícil de reproduzir. Normalizando
    aqui, hit e miss devolvem exatamente a mesma estrutura.

    `Decimal` (NUMERIC) vira float e `bytes` (BYTES) vira base64: nenhum dos dois
    é serializável em JSON, então sem isso a gravação no cache falharia em
    silêncio — e a resposta da própria tool também, já que ela também é
    serializada para JSON antes de chegar ao agente.
    """
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, dict):
        return {k: _normalize_bq_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_bq_value(v) for v in value]
    return value


class _QueryOutcome(NamedTuple):
    """Resultado da query mais a informação de se ele pode ser cacheado.

    `[]` vindo de uma tabela ausente é indistinguível de `[]` vindo de uma query
    que legitimamente não achou nada — e cachear o primeiro por uma hora congela
    uma falha transitória de infraestrutura em resposta vazia para todo mundo.
    Este flag é o que separa os dois casos.
    """

    rows: List[dict]
    cacheable: bool


class BigQueryTimeoutError(TimeoutError):
    """Estouro de prazo numa leitura do BigQuery.

    Subclasse de `TimeoutError` para não mudar o contrato de quem já captura
    o tipo embutido — mas precisa mesmo ser subclasse, e não o `TimeoutError`
    puro, por causa de `asyncio.futures._convert_future_exc`::

        if exc_class is concurrent.futures.TimeoutError:
            return exceptions.TimeoutError(*exc.args)

    A comparação é de identidade de classe. Uma exceção levantada dentro do
    executor cujo tipo seja *exatamente* `TimeoutError` (que em 3.11+ é o
    mesmo objeto que `concurrent.futures.TimeoutError`) é descartada e
    reconstruída do zero na travessia thread → event loop, junto com
    `__cause__` e `__traceback__`. Ou seja: o `from e` do outro lado da
    fronteira simplesmente não sobrevive. Com uma subclasse, o `else` daquele
    trecho devolve a exceção original intacta.
    """


class BigQueryQueryError(Exception):
    """Falha inesperada ao executar uma leitura no BigQuery.

    Separa o desfecho "não sei o que aconteceu" dos dois que o chamador
    consegue tratar: `GoogleAPIError` (falha conhecida de infraestrutura,
    repassada com o tipo original) e `TimeoutError` (estouro de prazo, venha
    ele do relógio do `wait_for` ou do relógio do `.result()`).

    Antes os três chegavam como `Exception` crua, o que obrigava todo chamador
    a olhar a *mensagem* para decidir o que fazer — e fazia a mesma condição
    (query lenta) chegar ora como `TimeoutError`, ora como `Exception`,
    dependendo de qual dos dois relógios disparasse primeiro.
    """


# ---------------------------------------------------------------------------
# Executor dedicado às leituras.
#
# O `wait_for` libera o `await` no prazo, mas não mata a thread: ela segue
# ocupada até a query terminar sozinha. Com o executor default do loop, essas
# threads presas são as mesmas que `save_response_in_bq_background`,
# `save_feedback_in_bq_background` e `save_cor_alert_in_bq_background` usam —
# ou seja, um BigQuery lento na *leitura* acabava enfileirando a gravação de
# log e de alerta do COR, que não têm nada a ver com o problema.
#
# Pool próprio e limitado: leitura travada só atrapalha leitura. E o limite é
# desejável, não um efeito colateral — quando ele satura, a espera pela vaga
# corre dentro do mesmo deadline da chamada, então o excesso vira TimeoutError
# rápido em vez de fila invisível.
# ---------------------------------------------------------------------------

_read_executor: ThreadPoolExecutor = None
_read_executor_lock = threading.Lock()


def _get_read_executor() -> ThreadPoolExecutor:
    """Devolve (criando na primeira vez) o pool de threads das leituras."""
    global _read_executor
    if _read_executor is not None:
        return _read_executor
    with _read_executor_lock:
        if _read_executor is None:
            max_workers = int(getattr(env, "BIGQUERY_READ_MAX_WORKERS", 8))
            _read_executor = ThreadPoolExecutor(
                max_workers=max_workers, thread_name_prefix="bq-read"
            )
    return _read_executor


def _shutdown_read_executor() -> None:
    """Encerra o pool no fim do processo, sem esperar query pendurada."""
    global _read_executor
    executor, _read_executor = _read_executor, None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


def _cancelar_job(query_job) -> None:
    """Pede ao BigQuery para cancelar um job cujo resultado ninguém vai mais ler.

    Estourado o prazo, o job continuaria varrendo bytes do lado do Google e
    sendo cobrado por isso — o cliente desistiu, o servidor não sabe disso. O
    cancelamento é best-effort de propósito: já estamos no caminho de erro e
    falhar aqui só substituiria o timeout (que é a informação útil) por outra
    exceção qualquer.
    """
    if query_job is None:
        return
    try:
        query_job.cancel()
    except Exception as e:
        logger.warning(f"Não foi possível cancelar o job do BigQuery: {e}")


def _execute_bigquery_query(
    query: str,
    query_parameters: list = None,
    page_size: int = None,
    timeout_seconds: float = None,
) -> _QueryOutcome:
    """Synchronous execution of BigQuery query, designed to run in thread executor."""
    default_page_size = getattr(env, "GOOGLE_BIGQUERY_PAGE_SIZE", 100)
    default_timeout = getattr(env, "BIGQUERY_TIMEOUT_SECONDS", 10.0)

    page_size = page_size if page_size is not None else default_page_size
    timeout_seconds = (
        timeout_seconds if timeout_seconds is not None else default_timeout
    )
    client = get_bigquery_client()

    tracer = get_tracer()
    query_job = None
    with tracer.start_as_current_span("bigquery.query") as span:
        span.set_attribute("bigquery.page_size", page_size)
        span.set_attribute("bigquery.query_length", len(query))
        try:
            logger.info(f"Executando query no BigQuery: {query[:100]}...")
            job_config = None
            if query_parameters:
                job_config = bigquery.QueryJobConfig(query_parameters=query_parameters)

            # O `timeout` aqui é o da chamada HTTP que *cria* o job, e é
            # distinto do `.result()` logo abaixo, que espera o job terminar.
            # Sem ele, uma submissão pendurada segurava a thread para sempre:
            # o `wait_for` do chamador devolvia o controle no prazo, mas a
            # vaga no pool só voltava quando a API respondesse.
            if job_config is not None:
                query_job = client.query(
                    query, job_config=job_config, timeout=timeout_seconds
                )
            else:
                query_job = client.query(query, timeout=timeout_seconds)

            results = query_job.result(page_size=page_size, timeout=timeout_seconds)

            rows = [
                {key: _normalize_bq_value(value) for key, value in row.items()}
                for row in results
            ]

            logger.info(f"Query executada com sucesso. {len(rows)} linhas retornadas.")
            span.set_attribute("bigquery.row_count", len(rows))
            span.set_attribute("bigquery.success", True)
            span.set_status(Status(StatusCode.OK))
            return _QueryOutcome(rows, cacheable=True)
        except NotFound as e:
            span.set_attribute("bigquery.row_count", 0)
            span.set_attribute("bigquery.table_not_found", True)
            span.set_attribute("bigquery.success", True)
            span.set_status(Status(StatusCode.OK))
            logger.warning(f"Tabela não encontrada no BigQuery: {str(e)}")
            # Degradação: devolve vazio para não derrubar a tool, mas marca como
            # não-cacheável. Tabela ausente costuma ser transitória (recriação,
            # permissão, tabela externa fora do ar) e cachear esse vazio por uma
            # hora transformaria o incidente em resposta errada muito depois de
            # ele ter passado.
            return _QueryOutcome([], cacheable=False)
        except TimeoutError as e:
            # É o que `query_job.result(timeout=...)` levanta quando o job não
            # termina no prazo (em 3.11+ `concurrent.futures.TimeoutError` é o
            # `TimeoutError` embutido). Precisa vir antes do `except Exception`
            # abaixo: sem este bloco, o estouro de prazo *interno* virava
            # `BigQueryQueryError` enquanto o estouro no `wait_for` do chamador
            # virava `TimeoutError` — a mesma condição com dois tipos, e qual
            # deles chegava dependia de qual relógio disparasse primeiro.
            span.set_attribute("bigquery.success", False)
            span.set_attribute("bigquery.timeout", True)
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            logger.error(
                f"Timeout de {timeout_seconds}s ao aguardar o job do BigQuery: {e}"
            )
            _cancelar_job(query_job)
            raise BigQueryTimeoutError(
                f"BigQuery query execution timed out after {timeout_seconds}s"
            ) from e
        except GoogleAPIError as e:
            span.set_attribute("bigquery.success", False)
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            logger.error(f"Erro ao executar query no BigQuery: {str(e)}")
            # Repassa a exceção original em vez de envolvê-la: é o tipo que
            # permite ao chamador separar falha conhecida de infraestrutura
            # (400 de tabela externa, 403 do Drive) de um bug nosso. Envolver
            # em `Exception` apagaria essa distinção.
            raise
        except Exception as e:
            span.set_attribute("bigquery.success", False)
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            logger.error(f"Erro ao executar query no BigQuery: {str(e)}")
            # `from e` preserva a causa: `BigQueryQueryError` diz "não era
            # falha conhecida nem prazo", e o traceback original continua
            # disponível para descobrir o que de fato era.
            raise BigQueryQueryError(
                f"Failed to execute BigQuery query: {str(e)}"
            ) from e


_CACHE_MISS = object()
# Distinto de `_CACHE_MISS`: "não tinha" e "não deu para perguntar" levam a
# decisões diferentes na hora de gravar.
_CACHE_UNAVAILABLE = object()


async def _cache_read(cache_key: str, span):
    """Lê do cache. Devolve `_CACHE_MISS` quando não há valor utilizável.

    Qualquer falha do Redis é degradação, não erro: o BigQuery continua sendo a
    fonte da verdade. O que muda em relação a só logar um warning é o atributo
    no span — sem ele, um cache 100% inoperante é indistinguível de um cache
    que só não teve acertos.
    """
    try:
        redis_client = await get_async_redis_client()
        if redis_client is None:
            span.set_attribute("cache.available", False)
            return _CACHE_MISS
        cached_data = await redis_client.get(cache_key)
        if cached_data is None:
            return _CACHE_MISS
        try:
            if isinstance(cached_data, bytes):
                cached_data = cached_data.decode("utf-8")
            return json.loads(cached_data)
        except ValueError as e:
            # `ValueError` cobre os dois modos de corrupção: `JSONDecodeError`
            # (texto que não é JSON) e `UnicodeDecodeError` (bytes que não são
            # UTF-8) são ambos subclasses dele.
            # Valor ilegível é MISS, não indisponibilidade. A diferença importa
            # porque `_CACHE_UNAVAILABLE` faz o chamador pular a gravação: a
            # chave envenenada sobreviveria até o TTL e *toda* requisição pagaria
            # uma query nesse intervalo. Como MISS, a query roda uma vez e
            # sobrescreve o lixo. O Redis aqui está saudável — ele respondeu.
            span.set_attribute("cache.corrupt_value", type(e).__name__)
            logger.warning(
                f"Valor inválido no cache do BigQuery (chave {cache_key}), "
                f"tratando como miss e sobrescrevendo: {e}"
            )
            return _CACHE_MISS
    except Exception as e:
        # Atributo próprio para leitura: quando o Redis está fora, a escrita
        # logo em seguida também falha, e um atributo único faria a segunda
        # apagar a primeira — escondendo justamente onde a degradação começou.
        span.set_attribute("cache.read_error", type(e).__name__)
        logger.warning(f"Erro ao ler cache do Redis para BigQuery: {e}")
        return _CACHE_UNAVAILABLE


async def _cache_write(
    cache_key: str, rows: List[dict], ttl: int, span, restante: float = None
) -> None:
    """Grava no cache, com jitter no TTL. Falha aqui nunca derruba a query.

    `restante` é o que sobrou do orçamento da chamada. Sem esse limite a
    gravação corria *fora* do prazo que `get_bigquery_result` promete: ela
    acontece depois de a query já ter respondido, então com o Redis mudo o
    tool call somava o timeout do BigQuery mais o timeout de socket do Redis
    (medido: +4,0s sobre um orçamento de 5s, porque o cliente ainda tenta
    reconectar). Com o limite, o teto da chamada volta a ser o timeout
    configurado, que era o critério do CHATR-125.

    Estourar o prazo aqui não é erro e não vira exceção: as linhas já estão na
    mão do chamador e serão devolvidas de qualquer jeito. O único efeito é a
    próxima requisição pagar outro miss. Cancelar no meio é seguro — o cliente
    do Redis derruba a conexão em `BaseException` justamente para não devolver
    ao pool uma conexão com resposta pendente.
    """
    if restante is not None and restante <= 0:
        span.set_attribute("cache.write_skipped", "budget_exhausted")
        return

    async def _gravar() -> None:
        redis_client = await get_async_redis_client()
        if redis_client is None:
            return
        serialized = json.dumps(rows, cls=CustomJSONEncoder)
        ttl_efetivo = _ttl_com_jitter(ttl)
        await redis_client.setex(cache_key, ttl_efetivo, serialized)
        span.set_attribute("cache.written", True)
        span.set_attribute("cache.ttl_seconds", ttl_efetivo)

    try:
        # `timeout=None` é "sem limite", então o caminho sem orçamento continua
        # se comportando como antes.
        await asyncio.wait_for(_gravar(), timeout=restante)
    except asyncio.TimeoutError:
        # Precisa vir antes do `except Exception`: `asyncio.TimeoutError` é
        # subclasse de `Exception` e cairia no ramo de erro do Redis, que
        # descreve outra coisa — o Redis pode estar perfeitamente saudável e
        # só não ter sobrado prazo.
        span.set_attribute("cache.write_timeout", True)
        logger.warning(
            f"Orçamento esgotado antes de gravar o cache do BigQuery "
            f"(chave {cache_key}); resultado devolvido sem cachear."
        )
    except Exception as e:
        span.set_attribute("cache.write_error", type(e).__name__)
        logger.warning(f"Erro ao gravar cache no Redis para BigQuery: {e}")


async def _run_query_com_timeout(
    query: str,
    query_parameters: list,
    page_size: int,
    timeout: float,
    orcamento: float = None,
) -> _QueryOutcome:
    """Roda a query no executor de leitura, limitada a `timeout` segundos.

    São dois números porque são duas coisas distintas:

    * `timeout` é o que *sobrou* do prazo — o cache e a fila do single-flight
      já podem ter consumido parte dele. É ele que limita a execução.
    * `orcamento` é o prazo configurado para a chamada inteira, e serve só
      para as mensagens. Quem lê um log ou trata a exceção quer saber que
      estourou o limite de 10s, não que restavam 9.87431s quando a query
      começou.

    O `except` aqui pega os dois relógios — o do `wait_for` e o do
    `.result()`, que chega pela thread — e os converte no mesmo `TimeoutError`
    com a mesma mensagem. Era essa a inconsistência do CHATR-125: a mesma
    condição chegava ao chamador com tipo diferente conforme quem disparasse
    primeiro, e como os dois usam o mesmo prazo, isso era decidido por corrida.
    """
    orcamento = timeout if orcamento is None else orcamento
    if timeout <= 0:
        # O prazo acabou antes de a query começar — normalmente esperando na
        # fila do single-flight. Levantar aqui evita mandar ao BigQuery um
        # trabalho cujo resultado ninguém mais vai esperar.
        raise BigQueryTimeoutError(
            f"BigQuery query execution timed out after {orcamento}s"
        )

    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(
                _get_read_executor(),
                _execute_bigquery_query,
                query,
                query_parameters,
                page_size,
                timeout,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError as e:
        logger.error(f"Timeout de {orcamento}s excedido ao executar query BigQuery")
        # `from e` preserva a cadeia: quando quem estourou foi o `.result()`,
        # `e` já carrega a exceção original do cliente do BigQuery.
        raise BigQueryTimeoutError(
            f"BigQuery query execution timed out after {orcamento}s"
        ) from e


@interceptor(source={"source": "mcp", "tool": "bigquery"})
async def get_bigquery_result(
    query: str,
    query_parameters: list = None,
    page_size: int = None,
    cache_ttl_seconds: int = None,
    timeout_seconds: float = None,
    cache_namespace: str = None,
    cache_key_parts: dict = None,
) -> List[dict]:
    """
    Executes a BigQuery query with caching and non-blocking executor execution.

    Args:
        query: SQL query to execute
        query_parameters: List of BigQuery QueryParameter objects
        page_size: Number of rows per page (optional, uses env default)
        cache_ttl_seconds: Cache TTL in seconds (0 to bypass cache)
        timeout_seconds: Query execution timeout in seconds
        cache_namespace: Prefixo semântico da chave de cache (ex.: "equipments").
            Sem ele a chave cai no hash do SQL — ver `_generate_cache_key`.
        cache_key_parts: Parâmetros semânticos que identificam a consulta
            (ex.: `{"plus8": ..., "cats": [...]}`).

    Returns:
        List of dictionaries with query results
    """
    default_ttl = getattr(env, "BIGQUERY_CACHE_TTL_SECONDS", 3600)
    default_timeout = getattr(env, "BIGQUERY_TIMEOUT_SECONDS", 10.0)

    ttl = cache_ttl_seconds if cache_ttl_seconds is not None else default_ttl
    timeout = timeout_seconds if timeout_seconds is not None else default_timeout

    # Um único orçamento para a chamada inteira: leitura de cache, espera na
    # fila do single-flight e execução da query saem todos daqui. É o que dá
    # ao tool call um teto de latência igual ao timeout configurado,
    # independente de quantas requisições disputem a mesma chave (CHATR-125).
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    tracer = get_tracer()
    with tracer.start_as_current_span("bigquery.read") as span:
        span.set_attribute("cache.enabled", ttl > 0)
        span.set_attribute("bigquery.timeout_budget_seconds", timeout)

        if ttl <= 0:
            span.set_attribute("cache.hit", False)
            outcome = await _run_query_com_timeout(
                query, query_parameters, page_size, deadline - loop.time(), timeout
            )
            return outcome.rows

        cache_key = _generate_cache_key(
            query, query_parameters, cache_namespace, cache_key_parts
        )
        span.set_attribute("cache.key", cache_key)

        cached = await _cache_read(cache_key, span)
        if cached is not _CACHE_MISS and cached is not _CACHE_UNAVAILABLE:
            span.set_attribute("cache.hit", True)
            logger.info(f"BigQuery cache hit para chave: {cache_key}")
            return cached

        # Redis que acabou de falhar na leitura vai falhar na escrita também, e
        # cada tentativa custa o timeout de socket inteiro. Pular a gravação
        # corta o pior caso pela metade — de outro modo toda requisição paga
        # dois timeouts enquanto o Redis estiver mudo.
        redis_indisponivel = cached is _CACHE_UNAVAILABLE

        # Só uma execução por chave dentro deste processo. Quem chegar junto
        # espera e é atendido pela leitura de cache logo abaixo, em vez de
        # disparar a mesma query em paralelo.
        async with _single_flight(cache_key, deadline):
            if not redis_indisponivel:
                cached = await _cache_read(cache_key, span)
                if cached is not _CACHE_MISS and cached is not _CACHE_UNAVAILABLE:
                    span.set_attribute("cache.hit", True)
                    span.set_attribute("cache.coalesced", True)
                    return cached

            span.set_attribute("cache.hit", False)
            outcome = await _run_query_com_timeout(
                query, query_parameters, page_size, deadline - loop.time(), timeout
            )

            if redis_indisponivel:
                span.set_attribute("cache.write_skipped", "redis_unavailable")
            elif not outcome.cacheable:
                span.set_attribute("cache.write_skipped", "degraded_result")
            else:
                # A gravação sai do mesmo orçamento da chamada: é o que mantém
                # o teto de latência do tool call igual ao timeout configurado.
                await _cache_write(
                    cache_key, outcome.rows, ttl, span, deadline - loop.time()
                )

            return outcome.rows


# ---------------------------------------------------------------------------
# Module-level initialisation — must come after flush_bigquery_batch_buffer is
# defined so the flush thread can call it safely.
# ---------------------------------------------------------------------------

# Flush remaining rows on clean process exit (atexit fires before interpreter
# shutdown; gunicorn/uvicorn trigger it on SIGTERM for each worker).
atexit.register(_stop_batch_flush_thread)

# Encerra o pool de leituras no mesmo momento (não espera query pendurada).
atexit.register(_shutdown_read_executor)

# Start the periodic background flush thread for the lifetime of this process.
_start_batch_flush_thread()
