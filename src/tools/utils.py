import json
from typing import Any, Dict

from src.config import env
from src.utils.log import logger
from src.utils.error_interceptor import interceptor
from src.utils.http_client import InterceptedHTTPClient
from src.utils.pii import redigir_credenciais


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


def _argumento(
    args: tuple, kwargs: dict, posicao: int, nome: str, padrao: Any = None
) -> Any:
    """Argumento da chamada por nome ou, na falta, pela posição na assinatura."""
    if nome in kwargs:
        return kwargs[nome]
    return args[posicao] if len(args) > posicao else padrao


def _forma_do_valor(valor: Any) -> Any:
    """
    A forma de um valor de `request_kwargs`, nunca o conteúdo.

    Dict vira os nomes das chaves (os campos do contrato da API, ou os nomes
    dos cabeçalhos) e a contagem de cada lista; lista vira a contagem;
    booleano e `None` ficam (o `verify` não diz nada sobre ninguém); o resto
    vira só o nome do tipo.
    """
    if valor is None or isinstance(valor, bool):
        return valor
    if isinstance(valor, dict):
        forma: Dict[str, Any] = {"campos": sorted(str(campo) for campo in valor)}
        for campo, item in valor.items():
            if isinstance(item, (list, tuple)):
                forma[f"qtd_{campo}"] = len(item)
        return forma
    if isinstance(valor, (list, tuple)):
        return {"tipo": "lista", "qtd": len(valor)}
    return {"tipo": type(valor).__name__}


def _input_body_do_internal_request(args: tuple, kwargs: dict) -> Dict[str, Any]:
    """
    `input_body` do `internal_request` no error interceptor (DEV-488).

    `request_kwargs` leva a credencial do serviço chamado (a `ChaveAcesso` da
    PGM em `data`, o token Bearer em `headers`) e os dados do contribuinte (CPF,
    CDA, execução fiscal, guia). A redação por padrão do interceptor não
    reconhece nenhuma credencial nem o formato de CDA, execução ou guia, então
    vai só a forma de cada argumento de `request_kwargs` (`_forma_do_valor`):
    nomes de campos e de cabeçalhos e contagens, nunca os valores. A URL vai
    sem credencial de query string.
    """
    url = _argumento(args, kwargs, 0, "url")
    method = _argumento(args, kwargs, 1, "method", "GET")
    request_kwargs = _argumento(args, kwargs, 2, "request_kwargs")

    if isinstance(request_kwargs, dict):
        forma_dos_kwargs: Any = {
            str(nome): _forma_do_valor(valor) for nome, valor in request_kwargs.items()
        }
    else:
        forma_dos_kwargs = _forma_do_valor(request_kwargs)

    return {
        "url": redigir_credenciais(url) if isinstance(url, str) else None,
        "method": method if isinstance(method, str) else None,
        "request_kwargs": forma_dos_kwargs,
    }


@interceptor(
    source={"source": "mcp", "tool": "internal_request"},
    extract_input_body=_input_body_do_internal_request,
)
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
            logger.error(f"Response text: {text[:500]}")  # Log first 500 chars
            # Check if it's a gateway timeout or other timeout-related error
            if (
                "504 Gateway Time-out" in text
                or "502 Bad Gateway" in text
                or "timeout" in text.lower()
            ):
                raise TimeoutError(f"Gateway timeout from {url}")
            raise
