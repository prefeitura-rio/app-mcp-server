import asyncio
from google.cloud import bigquery
from google.oauth2 import service_account
from typing import List
import base64
import json
import src.config.env as env
from datetime import datetime
import pytz
from src.utils.log import logger


def get_bigquery_client() -> bigquery.Client:
    """Get the BigQuery client.

    Returns:
        bigquery.Client: The BigQuery client.
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


def save_response_in_bq(
    data: dict,
    endpoint: str,
    dataset_id: str,
    table_id: str,
    project_id: str = "rj-iplanrio",
):
    table_full_name = f"{project_id}.{dataset_id}.{table_id}"
    logger.info(f"Salvando resposta no BigQuery: {table_full_name}")
    schema = [
        bigquery.SchemaField("datetime", "DATETIME", mode="NULLABLE"),
        bigquery.SchemaField("endpoint", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("data", "JSON", mode="NULLABLE"),
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


async def save_response_in_bq_background(data, endpoint, dataset_id, table_id):
    """
    Asynchronous wrapper for saving the response in BigQuery.
    """
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
    )
