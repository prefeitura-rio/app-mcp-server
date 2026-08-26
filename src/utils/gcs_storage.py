"""
Upload de arquivos no bucket de workflows e geração de signed URL.

Os fluxos que emitem guia de pagamento precisam entregar ao cidadão um link
para um arquivo que não existe em lugar nenhum: a página Pix montada na hora e
o PDF que a API da Prefeitura devolve em base64. Os dois seguem o mesmo
caminho — sobem para o bucket de workflows e saem como signed URL temporária.

Nada aqui é específico de IPTU: o bucket e as credenciais são os mesmos para
qualquer workflow, e o que varia (caminho do blob, content-type, validade) é
política de cada fluxo e entra por parâmetro.
"""

import base64
import datetime as dt
import json

from google.cloud import storage
from google.oauth2 import service_account

from src.config import env


def get_credentials_from_env() -> service_account.Credentials:
    """Credenciais da service account de workflows, guardada em base64."""
    info: dict = json.loads(base64.b64decode(env.WORKFLOWS_GCP_SERVICE_ACCOUNT))
    return service_account.Credentials.from_service_account_info(info)


def get_workflows_bucket():
    """Bucket onde ficam os arquivos temporários dos workflows."""
    client = storage.Client(credentials=get_credentials_from_env())
    return client.bucket(env.WORKFLOWS_GCS_BUCKET)


async def upload_to_gcs(
    conteudo: str | bytes,
    blob_path: str,
    content_type: str,
    ttl: dt.timedelta,
) -> str:
    """
    Sobe um arquivo para o bucket de workflows e devolve a signed URL.

    Args:
        conteudo: Texto ou bytes do arquivo. Quem tem base64 decodifica antes.
        blob_path: Caminho do blob dentro do bucket.
        content_type: Content-Type com que o arquivo será servido.
        ttl: Por quanto tempo a signed URL continua válida.

    Returns:
        URL assinada para acesso ao arquivo.
    """
    bucket = get_workflows_bucket()
    blob = bucket.blob(blob_path)
    blob.upload_from_string(conteudo, content_type=content_type)
    return blob.generate_signed_url(expiration=ttl)
