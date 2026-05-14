"""
Serviço para geração do link temporário da página Pix de IPTU.
"""

import base64
import datetime as dt
import json
import uuid
from typing import Optional

import httpx
from google.cloud import storage
from google.oauth2 import service_account
from loguru import logger

from src.config import env
from src.tools.multi_step_service.workflows.iptu_pagamento.pix_page import (
    build_pix_copy_page,
)
from src.utils.http_client import InterceptedHTTPClient


PIX_PAGE_TTL_HOURS = 24


def format_expires_at(expiration: dt.datetime) -> str:
    return (
        expiration.astimezone(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class IPTUPixPageService:
    def __init__(self, user_id: str = "unknown"):
        self.user_id = user_id

    def get_credentials_from_env(self) -> service_account.Credentials:
        info: dict = json.loads(base64.b64decode(env.WORKFLOWS_GCP_SERVICE_ACCOUNT))
        return service_account.Credentials.from_service_account_info(info)

    def _get_workflows_gcs_bucket(self):
        google_credentials = self.get_credentials_from_env()
        client = storage.Client(credentials=google_credentials)
        return client.bucket(env.WORKFLOWS_GCS_BUCKET)

    async def upload_pix_copy_page_to_gcs(
        self, qr_code_pix: str, pix_code: Optional[str]
    ) -> str:
        bucket = self._get_workflows_gcs_bucket()
        page_html = build_pix_copy_page(qr_code_pix=qr_code_pix, pix_code=pix_code)
        blob = bucket.blob(f"iptu/qrcode-pix/{uuid.uuid4()}.html")
        blob.upload_from_string(page_html, content_type="text/html; charset=utf-8")
        return blob.generate_signed_url(
            expiration=dt.timedelta(hours=PIX_PAGE_TTL_HOURS)
        )

    async def create_pix_copy_page_url(
        self,
        qr_code_pix: str,
        pix_code: Optional[str],
    ) -> Optional[str]:
        expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
            hours=PIX_PAGE_TTL_HOURS
        )
        signed_url = await self.upload_pix_copy_page_to_gcs(
            qr_code_pix=qr_code_pix,
            pix_code=pix_code,
        )
        short_url = await self.get_short_url(
            url=signed_url,
            title="Pix para pagamento de cotas do IPTU",
            description="Página para copiar o código Pix das cotas selecionadas.",
            expires_at=format_expires_at(expires_at),
        )
        return short_url or signed_url

    async def get_short_url(
        self,
        url: str,
        title: str,
        description: str,
        expires_at: Optional[str] = None,
        image_url: Optional[str] = None,
        short_path: Optional[str] = None,
    ) -> Optional[str]:
        api_url = f"{env.SHORT_API_URL}/link/api/urls"
        headers = {
            "Authorization": f"Bearer {env.SHORT_API_TOKEN}",
            "Content-Type": "application/json",
        }
        payload = {
            "description": description,
            "destination": url,
            "title": title,
        }
        if expires_at:
            payload["expires_at"] = expires_at
        if image_url:
            payload["image_url"] = image_url
        if short_path:
            payload["short_path"] = short_path

        try:
            async with InterceptedHTTPClient(
                user_id=self.user_id,
                source={
                    "source": "mcp",
                    "tool": "multi_step_service",
                    "workflow": "iptu_pagamento",
                },
            ) as client:
                response = await client.post(api_url, json=payload, headers=headers)
                if response.status_code == 200 or response.status_code == 201:
                    data = response.json()
                    logger.info(f"URL shortened successfully: {data}")
                    return f"{env.SHORT_API_URL}/link/{data['short_path']}"

                logger.error(f"Erro HTTP ao encurtar URL: {response.status_code}")
                return None
        except httpx.TimeoutException:
            logger.error("Timeout ao encurtar URL")
            return None
        except Exception as e:
            logger.error(f"Erro ao encurtar URL: {e}")
            return None
