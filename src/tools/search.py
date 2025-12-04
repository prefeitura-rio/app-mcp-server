import asyncio

from src.tools.google_search.gemini_service import gemini_service
from src.utils.bigquery import save_response_in_bq_background
from src.utils.typesense_api import HubSearchRequest, hub_search

from src.config import env

from src.config import env



async def get_google_search(query: str):
    """
    Get google search results
    """
    final_response = {}
    bq_response = {}

    # hub_request = HubSearchRequest(
    #     q=query,
    #     type="hybrid",
    #     threshold_semantic=0.7,
    #     # threshold_keyword=1,
    #     threshold_hybrid=0.7,
    #     # threshold_ai=0.85,
    #     page=1,
    #     per_page=5,
    #     alpha=0.7,
    # )

    # if hub_request.type == "ai":
    #     hub_request.generate_scores = True

    # response_typesense = await hub_search(request=hub_request)

    # if (
    #     response_typesense
    #     and response_typesense.get("results")
    #     and len(response_typesense["results"]) > 0
    # ):
    #     final_response = {
    #         "response": response_typesense.get("results_clean"),
    #     }
    #     bq_response = {"source": "typesense", "response": response_typesense}
    # else:
    response_google = await gemini_service.google_search(
        query=query,
        model=env.GEMINI_MODEL,
        temperature=0.0,
        retry_attempts=1,
    )

    final_response = {
        "text": response_google.get("text"),
        "sources": response_google.get("sources"),
        "web_search_queries": response_google.get("web_search_queries"),
        "id": response_google.get("id"),
    }
    bq_response = {"source": "google", "response": response_google}

    asyncio.create_task(
        save_response_in_bq_background(
            data=bq_response,
            endpoint="/tools/google_search",
            dataset_id="brutos_eai_logs",
            table_id="mcp",
        )
    )

    return final_response
