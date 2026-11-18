import json
from src.config import env
from src.utils.log import logger
from src.utils.error_interceptor import interceptor
from src.utils.http_client import InterceptedHTTPClient


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


@interceptor(source={"source": "mcp", "tool": "internal_request"})
async def internal_request(
    url: str,
    method: str = "GET",
    request_kwargs: dict | None = None,
) -> dict | None:
    """
    Uses chatbot-integrations for making requests through the internal network.

    Args:
        url (str): The URL to be requested.
        method (str, optional): The HTTP method. Defaults to "GET".
        request_kwargs (dict, optional): The request kwargs. Defaults to None.

    Returns:
        dict: The response JSON.
    """
    request_kwargs = request_kwargs or {}
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

    async with InterceptedHTTPClient(
        user_id="unknown",
        source={"source": "mcp", "tool": "internal_request"},
        timeout=600.0,
    ) as client:
        response = await client.post(integrations_url, headers=headers, content=payload)
        text = response.text
        if not text:
            logger.warning(f"Empty response from {url}")
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON from {url}: {e}")
            # Só a forma do corpo, nunca o texto (DEV-495).
            logger.error(
                {
                    "event": "internal_request_resposta_nao_json",
                    "resposta": resumir_resposta_nao_json(response, text),
                }
            )
            if _parece_gateway_timeout(text):
                raise TimeoutError(f"Gateway timeout from {url}")
            raise


def _parece_gateway_timeout(texto: str) -> bool:
    """Corpo de erro de gateway ou de timeout do proxy (502/504, CHATR-297)."""
    return (
        "504 Gateway Time-out" in texto
        or "502 Bad Gateway" in texto
        or "timeout" in texto.lower()
    )


def resumir_resposta_nao_json(response: object, texto: str) -> dict[str, object]:
    """
    Resumo para log de uma resposta que não é JSON: status HTTP, tamanho, tipo
    de conteúdo e se parece HTML ou erro de gateway/timeout. Nunca o texto.

    O corpo pode ser o HTML de erro do proxy ou uma resposta de texto do
    serviço chamado (a PGM), com CDA, execução fiscal, guia e nome do
    contribuinte, que a redação global de PII não reconhece (DEV-495). Aceita
    qualquer objeto de resposta sem levantar: uma exceção aqui trocaria o erro
    original por outro.
    """
    status_code = getattr(response, "status_code", None)
    headers = getattr(response, "headers", None)
    content_type = None
    if headers is not None and hasattr(headers, "get"):
        bruto = headers.get("content-type")
        if isinstance(bruto, str):
            content_type = bruto.split(";", 1)[0].strip().lower()[:100] or None
    inicio = texto.lstrip()[:1000].lower()
    return {
        "status_code": status_code if isinstance(status_code, int) else None,
        "tamanho": len(texto),
        "content_type": content_type,
        "parece_html": inicio.startswith("<!doctype html") or "<html" in inicio,
        "parece_gateway_timeout": _parece_gateway_timeout(texto),
    }
