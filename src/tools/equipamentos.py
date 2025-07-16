from typing import Optional, List
import json
from src.utils.eai_api import eai_api


def get_equipments_categories() -> str:
    """
    Get allEquipaments categories
    """
    categories = eai_api.request_api(path="/external/tools/equipments_category")
    return json.dumps(categories["categorias"], indent=4, ensure_ascii=False)


def get_equipments(address: str, categories: Optional[List[str]] = []) -> str:
    """
    Get Equipaments by address
    """
    params = {"address": address, "category": categories}
    equipments = eai_api.request_api(path=f"/external/tools/equipments", params=params)
    return json.dumps(equipments["equipamentos"], indent=4, ensure_ascii=False)


def get_equipments_instructions() -> str:
    """
    Get Equipaments instructions
    """
    instructions = eai_api.request_api(path="/external/tools/equipments_instructions")
    return json.dumps(instructions, indent=4, ensure_ascii=False)
