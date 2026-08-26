"""
Encurtador de URL da Prefeitura.

As signed URLs do GCS são longas e carregam a assinatura na query string — não
servem para mandar ao cidadão numa conversa. O encurtador interno troca a URL
por um link curto, com título e descrição próprios (o que aparece no preview do
WhatsApp) e validade própria.

Falha aqui não é fatal: todo chamador tem a URL original como fallback, então a
função devolve `None` em vez de propagar exceção.
"""

import datetime as dt
from typing import Any, Dict, Optional

import httpx
from loguru import logger

from src.config import env
from src.utils.http_client import DEFAULT_ERROR_STATUS_CODES, InterceptedHTTPClient


def format_expires_at(expiration: dt.datetime) -> str:
    """
    Formata o vencimento no formato que o encurtador espera (UTC, sufixo Z).

    Exige datetime com timezone: para um naive, `astimezone` assumiria o fuso da
    máquina e o vencimento do link passaria a depender do TZ do container.
    """
    if expiration.tzinfo is None:
        raise ValueError("expiration precisa ter timezone (use dt.timezone.utc)")
    return (
        expiration.astimezone(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


async def get_short_url(
    url: str,
    title: str,
    description: str,
    *,
    user_id: str,
    source: Dict[str, Any],
    expires_at: Optional[str] = None,
    image_url: Optional[str] = None,
    short_path: Optional[str] = None,
) -> Optional[str]:
    """
    Encurta uma URL no encurtador da Prefeitura.

    Args:
        url: URL de destino, para onde o link curto aponta.
        title: Título do link, exibido no preview.
        description: Descrição do link, exibida no preview.
        user_id: Usuário do atendimento, para o interceptor de erros.
        source: Origem da chamada, no formato do InterceptedHTTPClient. Cada
            workflow tem o seu — não é derivável aqui.
        expires_at: Vencimento do link, via `format_expires_at`.
        image_url: Imagem do preview.
        short_path: Caminho curto desejado, em vez de um gerado.

    Returns:
        A URL encurtada, ou None se o encurtamento falhar.
    """
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
            user_id=user_id,
            source=source,
        ) as client:
            # O interceptor só reporta status HTTP quando recebe a lista de
            # códigos: sem ela, encurtador fora do ar (401 de token, 5xx) falha
            # em silêncio — o cidadão recebe a signed URL crua e o monitoramento
            # não vê nada. Exceções de transporte o client já reporta sozinho.
            response = await client.post(
                api_url,
                json=payload,
                headers=headers,
                error_status_codes=DEFAULT_ERROR_STATUS_CODES,
            )
            if response.status_code == 200 or response.status_code == 201:
                data = response.json()
                # Sem o corpo da resposta: ele traz o short_path, ou seja, o link
                # direto para a guia daquele contribuinte (CHATR-174 D3).
                logger.info("URL encurtada com sucesso")
                return f"{env.SHORT_API_URL}/link/{data['short_path']}"

            logger.error(f"Erro HTTP ao encurtar URL: {response.status_code}")
            return None
    except httpx.TimeoutException:
        logger.error("Timeout ao encurtar URL")
        return None
    except Exception as e:
        logger.error(f"Erro ao encurtar URL: {e}")
        return None
