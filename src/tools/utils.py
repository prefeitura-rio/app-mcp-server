import json
from src.config import env
from src.utils.log import logger
import aiohttp

def get_integrations_url(endpoint: str) -> str:
    """
    Returns the URL of the endpoint in the integrations service.
    """
    base_url = env.CHATBOT_INTEGRATIONS_URL
    if base_url.endswith("/"):
        base_url = base_url[:-1]
    if endpoint.startswith("/"):
        endpoint = endpoint[1:]
    logger.info(f"Base URL: {base_url}")
    logger.info(f"Endpoint: {endpoint}")
    logger.info(f"URL: {base_url}/{endpoint}")
    return f"{base_url}/{endpoint}"


async def internal_request(
    url: str,
    method: str = "GET",
    request_kwargs: dict = {},
) -> aiohttp.ClientResponse:
    """
    Uses chatbot-integrations for making requests through the internal network.

    Args:
        url (str): The URL to be requested.
        method (str, optional): The HTTP method. Defaults to "GET".
        request_kwargs (dict, optional): The request kwargs. Defaults to {}.

    Returns:
        aiohttp.ClientResponse: The response object.
    """
    integrations_url = get_integrations_url("request")
    payload = json.dumps(
        {
            "url": url,
            "method": method,
            "request_kwargs": request_kwargs,
        }
    )
    key = env.CHATBOT_INTEGRATIONS_KEY
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    }
    async with aiohttp.ClientSession() as session:
        async with session.request(
            "POST", integrations_url, headers=headers, data=payload
        ) as response:
            return await response.json(content_type=None)