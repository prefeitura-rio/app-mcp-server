from typing import Optional, List

from src.utils.eai_api import eai_api


def get_equipments_categories():
    """
    Get allEquipaments categories
    """
    return eai_api.request_api(path="/external/tools/equipments_category")


def get_equipments(address: str, category: Optional[List[str]] = []):
    """
    Get Equipaments by address
    """
    params = {"address": address}
    return eai_api.request_api(path=f"/external/tools/equipments", params=params)


def get_equipments_instructions():
    """
    Get Equipaments instructions
    """
    return eai_api.request_api(path="/external/tools/equipments_instructions")
