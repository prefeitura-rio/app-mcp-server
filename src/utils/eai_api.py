from src.config import env
import requests
import json


class EAIApi:
    def __init__(self):
        self.base_url = env.EAI_AGENT_URL
        self.headers = {"Authorization": f"Bearer {env.EAI_AGENT_TOKEN}"}

    def request_api(self, path: str, payload: dict = None, params: dict = None):
        url = f"{self.base_url}{path}"
        print(f"Requesting {url}")

        response = requests.get(url, headers=self.headers, json=payload, params=params)
        response.raise_for_status()
        return json.dumps(response.json(), indent=4, ensure_ascii=False)


eai_api = EAIApi()
