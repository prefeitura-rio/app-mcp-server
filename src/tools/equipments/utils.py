import json
import datetime

import requests

from src.config import env
import src.tools.equipments.openlocationcode as olc
from src.utils.log import logger


class CustomJSONEncoder(json.JSONEncoder):
    """
    JSON Encoder customizado que sabe como converter objetos
    de data, hora e data/hora do Python para strings no padrão ISO 8601.
    """

    def default(self, obj):
        # Se o objeto for uma instância de datetime, date ou time...
        if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
            # ... converta-o para uma string no formato ISO.
            return obj.isoformat()

        # Para qualquer outro tipo, deixe o encoder padrão fazer o trabalho.
        return super().default(obj)


def get_coords_from_nominatim_api(address: str):
    params = {"q": address, "format": "json", "addressdetails": 1, "limit": 1}
    headers = {
        "User-Agent": "YourAppName/1.0 (your.email@example.com)"  # Required by Nominatim
    }
    response = requests.get(env.NOMINATIM_API_URL, params=params, headers=headers)
    response.raise_for_status()
    data = response.json()

    if data:
        coords = {
            "lat": float(data[0]["lat"]),
            "lng": float(data[0]["lon"]),
            "address": data[0]["display_name"],
        }
        coords["provider"] = "Nominatim"
        return coords
    return {}


def get_coords_from_google_maps_api(address: str):
    address = address + " - Rio de Janeiro, RJ"
    params = {"address": address, "key": env.GOOGLE_MAPS_API_KEY}
    response = requests.get(env.GOOGLE_MAPS_API_URL, params=params)
    data = response.json()
    if data["status"] == "OK":
        coords = data["results"][0]["geometry"]["location"]
        coords["address"] = data["results"][0]["formatted_address"]
        coords["provider"] = "Google Maps"
        return coords
    return {}


def get_plus8_coords_from_address(address: str):
    """Get the plus8 from an address.

    Args:
        address (str): The address to get the plus8 from.

    Returns:
        str: The plus8 from the address.
    """
    # try to use nominatim api
    coords = get_coords_from_nominatim_api(address=address)
    if coords == {}:
        coords = get_coords_from_google_maps_api(address=address)
    if coords == {}:
        logger.error("No coords from nominatim or google maps, returning None")
        return None

    coords_info = json.dumps(coords, ensure_ascii=False, indent=2)
    logger.info(f"\nGeolocated info:\n {coords_info}")
    plus8 = olc.encode(latitude=coords["lat"], longitude=coords["lng"], codeLength=8)
    logger.info(f"Encoded plus8 {plus8}")
    return plus8, coords
