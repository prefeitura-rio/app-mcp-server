from src.utils.eai_api import eai_api


def get_google_search(query: str):
    """
    Get google search results
    """
    params = {"query": query}
    return eai_api.request_api(path=f"/letta/tools/google_search", params=params)
