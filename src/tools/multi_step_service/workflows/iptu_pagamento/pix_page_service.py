"""
Serviço para geração do link temporário da página Pix de IPTU.
"""

import datetime as dt
import uuid
from typing import Optional

from src.tools.multi_step_service.workflows.iptu_pagamento.pix_page import (
    build_pix_copy_page,
)
from src.utils.gcs_storage import upload_to_gcs
from src.utils.short_url import format_expires_at, get_short_url


PIX_PAGE_TTL_HOURS = 24

ERROR_SOURCE = {
    "source": "mcp",
    "tool": "multi_step_service",
    "workflow": "iptu_pagamento",
}


class IPTUPixPageService:
    def __init__(self, user_id: str = "unknown"):
        self.user_id = user_id

    async def upload_pix_copy_page_to_gcs(
        self, qr_code_pix: str, pix_code: Optional[str]
    ) -> str:
        page_html = build_pix_copy_page(qr_code_pix=qr_code_pix, pix_code=pix_code)
        return await upload_to_gcs(
            conteudo=page_html,
            blob_path=f"iptu/qrcode-pix/{uuid.uuid4()}.html",
            content_type="text/html; charset=utf-8",
            ttl=dt.timedelta(hours=PIX_PAGE_TTL_HOURS),
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
        short_url = await get_short_url(
            url=signed_url,
            title="Pix para pagamento de cotas do IPTU",
            description="Página para copiar o código Pix das cotas selecionadas.",
            user_id=self.user_id,
            source=ERROR_SOURCE,
            expires_at=format_expires_at(expires_at),
        )
        return short_url or signed_url
