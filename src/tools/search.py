import asyncio

from src.tools.google_search.gemini_service import gemini_service
from src.utils.bigquery import save_response_in_bq_background


async def get_google_search(query: str):
    """
    Get google search results
    """
    response = await gemini_service.google_search(
        query=query,
        model="gemini-2.5-flash-lite-preview-06-17",
        temperature=0.0,
        retry_attempts=3,
    )

    asyncio.create_task(
        save_response_in_bq_background(
            data=response,
            endpoint="/tools/google_search",
            dataset_id="brutos_eai_logs",
            table_id="mcp",
        )
    )

    return {
        "text": response.get("text"),
        "sources": response.get("sources"),
        "web_search_queries": response.get("web_search_queries"),
        "id": response.get("id"),
    }
