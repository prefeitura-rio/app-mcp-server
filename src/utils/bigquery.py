import asyncio
from google.cloud import bigquery
from google.api_core.exceptions import NotFound
from google.oauth2 import service_account
from typing import List
import base64
import json
import src.config.env as env
from datetime import datetime, date
import pytz
from src.utils.log import logger
from src.utils.error_interceptor import interceptor


def get_bigquery_client() -> bigquery.Client:
    """Get the BigQuery client.

    Returns:
        bigquery.Client: The BigQuery client
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
    schema = [
        bigquery.SchemaField("datetime", "DATETIME", mode="NULLABLE"),
        bigquery.SchemaField("endpoint", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("data", "JSON", mode="NULLABLE"),
        bigquery.SchemaField("environment", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("data_particao", "DATE", mode="NULLABLE"),
    ]

    job_config = bigquery.LoadJobConfig(
        schema=schema,
        # Optionally, set the write disposition. BigQuery appends loaded rows
        # to an existing table by default, but with WRITE_TRUNCATE write
        # disposition it replaces the table with the loaded data.
        write_disposition="WRITE_APPEND",
        time_partitioning=bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY,
            field="data_particao",  # name of column to use for partitioning
        ),
    )
    datetime_to_save = get_datetime()
    data_to_save = {
        "datetime": datetime_to_save,
        "endpoint": endpoint,
        "data": data,
        "environment": env_value,
        "data_particao": datetime_to_save.split("T")[0],
    }
    json_data = json.loads(json.dumps([data_to_save]))
    client = get_bigquery_client()

    try:
        job = client.load_table_from_json(
            json_data, table_full_name, job_config=job_config
        )
        job.result()
        # logger.info(f"Resposta salva no BigQuery: {table_full_name}")
    except Exception:
        raise Exception(json_data)


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
    extract_user_id=lambda args, kwargs: kwargs.get("user_id") or (args[0] if args else "unknown"),
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

    schema = [
        bigquery.SchemaField("user_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("feedback", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("environment", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("timestamp", "DATETIME", mode="REQUIRED"),
        bigquery.SchemaField("data_particao", "DATE", mode="NULLABLE"),
    ]

    job_config = bigquery.LoadJobConfig(
        schema=schema,
        write_disposition="WRITE_APPEND",
        create_disposition="CREATE_IF_NEEDED",
        time_partitioning=bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY,
            field="data_particao",
        ),
    )

    data_to_save = {
        "user_id": user_id,
        "feedback": feedback,
        "environment": environment,
        "timestamp": timestamp,
        "data_particao": timestamp.split("T")[0],
    }

    json_data = json.loads(json.dumps([data_to_save]))
    client = get_bigquery_client()

    try:
        job = client.load_table_from_json(
            json_data, table_full_name, job_config=job_config
        )
        job.result()
        logger.info(f"Feedback salvo no BigQuery: {table_full_name}")
    except Exception as e:
        logger.error(f"Erro ao salvar feedback no BigQuery: {str(e)}")
        raise Exception(f"Failed to save feedback: {str(e)}")


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
    extract_user_id=lambda args, kwargs: kwargs.get("user_id") or (args[1] if len(args) > 1 else "unknown"),
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

    schema = [
        bigquery.SchemaField("alert_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("user_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("alert_type", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("severity", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("description", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("address", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("latitude", "FLOAT", mode="NULLABLE"),
        bigquery.SchemaField("longitude", "FLOAT", mode="NULLABLE"),
        bigquery.SchemaField("created_at", "DATETIME", mode="REQUIRED"),
        bigquery.SchemaField("environment", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("data_particao", "DATE", mode="NULLABLE"),
    ]

    job_config = bigquery.LoadJobConfig(
        schema=schema,
        write_disposition="WRITE_APPEND",
        create_disposition="CREATE_IF_NEEDED",
        time_partitioning=bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY,
            field="data_particao",
        ),
    )

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

    try:
        job = client.load_table_from_json(
            json_data, table_full_name, job_config=job_config
        )
        job.result()
        logger.info(f"Alerta COR salvo no BigQuery: {table_full_name}")
    except Exception as e:
        logger.error(f"Erro ao salvar alerta COR no BigQuery: {str(e)}")
        raise Exception(f"Failed to save COR alert: {str(e)}")


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
    dataset_id: str = "brutos_eai_logs",
    table_id: str = "cor_alerts",
):
    """
    Asynchronous wrapper for saving COR alert in BigQuery.
    Catches and logs exceptions to prevent crashing background tasks.
    """
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
            dataset_id,
            table_id,
        )
    except Exception:
        logger.exception(
            f"Failed to save COR alert to BigQuery in background for alert_id: {alert_id}"
        )


@interceptor(source={"source": "mcp", "tool": "bigquery"})
def get_bigquery_result(query: str, page_size: int = None) -> List[dict]:
    """
    Executes a BigQuery query and returns results as a list of dictionaries.

    Args:
        query: SQL query to execute
        page_size: Number of rows per page (optional, uses env default)

    Returns:
        List of dictionaries with query results
    """
    from src.config.env import GOOGLE_BIGQUERY_PAGE_SIZE

    page_size = page_size if page_size is not None else GOOGLE_BIGQUERY_PAGE_SIZE
    client = get_bigquery_client()

    try:
        logger.info(f"Executando query no BigQuery: {query[:100]}...")
        query_job = client.query(query)
        results = query_job.result(page_size=page_size)

        # Convert results to list of dictionaries
        rows = []
        for row in results:
            row_dict = {}
            for key, value in row.items():
                # Convert datetime/date objects to ISO format strings for JSON serialization
                if isinstance(value, (datetime, date)):
                    row_dict[key] = value.isoformat()
                else:
                    row_dict[key] = value
            rows.append(row_dict)

        logger.info(f"Query executada com sucesso. {len(rows)} linhas retornadas.")
        return rows
    except NotFound as e:
        logger.warning(f"Tabela não encontrada no BigQuery: {str(e)}")
        # Return empty list when table doesn't exist yet - allows graceful degradation
        return []
    except Exception as e:
        logger.error(f"Erro ao executar query no BigQuery: {str(e)}")
        raise Exception(f"Failed to execute BigQuery query: {str(e)}")
