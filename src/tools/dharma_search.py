import aiohttp
from src.config.env import DHARMA_API_KEY

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
    
    timeout = aiohttp.ClientTimeout(total=120)
    
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers, json=payload) as response:
            response.raise_for_status()
            return await response.json()
