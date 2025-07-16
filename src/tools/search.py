from src.utils.eai_api import eai_api
import json


def get_google_search(query: str):
    """
    Get google search results
    """
    params = {"query": query}
    search = eai_api.request_api(path=f"/letta/tools/google_search", params=params)
    return json.dumps(search, indent=4, ensure_ascii=False)
