from src.config import env
import requests
import json


class Equipamentos:
    def __init__(self):
        self.base_url = env.EAI_AGENT_URL
        self.headers = {"Authorization": f"Bearer {env.EAI_AGENT_TOKEN}"}

    def request_api(self, path: str, payload: dict = None, params: dict = None):
        url = f"{self.base_url}{path}"
        print(f"Requesting {url}")

        response = requests.get(url, headers=self.headers, json=payload, params=params)
        response.raise_for_status()
        return json.dumps(response.json(), indent=4)


equipaments = Equipamentos()


def get_equipaments_categories():
    """
    Get allEquipaments categories
    """
    return equipaments.request_api(path="/external/tools/equipments_category")


def get_equipaments(address: str):
    """
    Get Equipaments by address
    """
    params = {"address": address}
    return equipaments.request_api(path=f"/external/tools/equipments", params=params)


def get_google_search(query: str):
    """
    Get google search results
    """
    params = {"query": query}
    return equipaments.request_api(path=f"/letta/tools/google_search", params=params)
