import aiohttp
from src.config.env import SURKAI_API_KEY

async def surkai_search(query: str, k: int = 6, lang: str = "pt-BR"):
    """
    Busca informações usando a API Surkai
    
    Args:
        query: Consulta a ser pesquisada
        k: Número de resultados (padrão: 6)
        lang: Idioma da busca (padrão: pt-BR)
    
    Returns:
        dict: Resposta da API contendo summary e sources
    """
    url = "https://services.staging.app.dados.rio/eai-agent-surkai/api/v1/web_search"
    
    headers = {
        "Authorization": f"Bearer {SURKAI_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "k": k,
        "lang": lang,
        "query": query
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as response:
            response.raise_for_status()
            return await response.json()
