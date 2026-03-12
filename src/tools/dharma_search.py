from src.config.env import DHARMA_API_KEY
from src.utils.error_interceptor import interceptor
from src.utils.http_client import InterceptedHTTPClient


@interceptor(source={"source": "mcp", "tool": "dharma_search"})
async def dharma_search(query: str):
    """
    Busca informações usando a API Dharma

    Args:
        query: Mensagem/consulta a ser enviada para o assistente de IA

    Returns:
        dict: Resposta da API contendo message, documents e metadata
    """
    url = "http://dev-chat-iplan-lb-1628802729.us-east-1.elb.amazonaws.com/v1/chats"

    headers = {
        "Authorization": f"Bearer {DHARMA_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "message": query
    }

    async with InterceptedHTTPClient(
        user_id="unknown",
        source={"source": "mcp", "tool": "dharma_search"},
        timeout=120.0
    ) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()
