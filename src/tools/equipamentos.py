from src.config import env
import requests
import json

class Equipamentos:
    def __init__(self):
        self.base_url = env.EAI_AGENT_URL
        self.headers = {
            "Authorization": f"Bearer {env.EAI_AGENT_TOKEN}"
        }

    def request_api(self, path:str, payload:dict={}):
        url = f"{self.base_url}{path}"
        response = requests.get(url, headers=self.headers, json=payload)
        return json.dumps(response.json(), indent=4)
    
    def get_equipaments_categories(self):
        """
        Get allEquipaments categories
        """
        return self.request_api(path="/external/tools/equipments_category")

    def get_equipaments(self, address:str):
        """
        Get allEquipaments by address
        """
        payload = {
            "address": address
        }
        return self.request_api(path=f"/external/tools/equipments?address={address}")
