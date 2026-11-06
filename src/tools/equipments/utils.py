import json
from typing import Tuple, Optional

import httpx

from src.config import env
import src.tools.equipments.openlocationcode as olc
from src.utils.http_client import InterceptedHTTPClient
from src.tools.cor_alert_tools import (
    _extract_google_neighborhood,
    normalize_neighborhood,
)

# from src.utils.log import logger


def get_coords_from_nominatim_api(address: str) -> dict:
    params = {"q": address, "format": "json", "addressdetails": 1, "limit": 1}
    headers = {"User-Agent": "RioMCPServer/1.0 (equipments@rio)"}
    with InterceptedHTTPClient(
        user_id="unknown",
        source={
            "source": "mcp",
            "tool": "equipments",
            "function": "get_coords_from_nominatim_api",
        },
        # O endereço vai nos `params`: no report da falha, só metadado (DEV-411).
        free_text_fields=("q",),
        sync=True,
        timeout=10.0,
    ) as client:
        response = client.get_sync(
            env.NOMINATIM_API_URL, params=params, headers=headers
        )
        # Sem `raise_for_status`: a mensagem dele traz a URL com o endereço na
        # query, e ela chega ao error interceptor pelo `@interceptor` de quem
        # chama (DEV-411). Do erro fica só o status; como no `raise_for_status`,
        # qualquer resposta fora de 2xx levanta.
        if not response.is_success:
            raise httpx.HTTPStatusError(
                f"Nominatim respondeu HTTP {response.status_code}",
                request=response.request,
                response=response,
            )
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


def get_coords_from_google_maps_api(address: str) -> dict:
    address = address + " - Rio de Janeiro, RJ"
    params = {"address": address, "key": env.GOOGLE_MAPS_API_KEY}
    with InterceptedHTTPClient(
        user_id="unknown",
        source={
            "source": "mcp",
            "tool": "equipments",
            "function": "get_coords_from_google_maps_api",
        },
        # O endereço vai nos `params`: no report da falha, só metadado (DEV-411).
        free_text_fields=("address",),
        sync=True,
        timeout=10.0,
    ) as client:
        response = client.get_sync(env.GOOGLE_MAPS_API_URL, params=params)
        data = response.json()
    if data["status"] == "OK":
        first_result = data["results"][0]
        coords = first_result["geometry"]["location"]
        coords["address"] = first_result["formatted_address"]
        coords["provider"] = "Google Maps"

        # Extrair bairro
        bairro_raw = _extract_google_neighborhood(first_result)
        coords["bairro_raw"] = bairro_raw if bairro_raw else None
        coords["bairro_normalizado"] = (
            normalize_neighborhood(bairro_raw) if bairro_raw else None
        )

        return coords
    return {}


def get_plus8_coords_from_address(address: str) -> Tuple[Optional[str], Optional[dict]]:
    """Get the plus8 from an address.

    Args:
        address (str): The address to get the plus8 from.

    Returns:
        str: The plus8 from the address.
    """
    # try to use nominatim api
    # coords = get_coords_from_nominatim_api(address=address)
    coords = {}
    if coords == {}:
        coords = get_coords_from_google_maps_api(address=address)
    if coords == {}:
        # logger.error("No coords from nominatim or google maps, returning None")
        return None, None

    json.dumps(coords, ensure_ascii=False, indent=2)
    # logger.info(f"\nGeolocated info:\n {coords_info}")
    plus8 = olc.encode(latitude=coords["lat"], longitude=coords["lng"], codeLength=8)
    # logger.info(f"Encoded plus8 {plus8}")
    return plus8, coords
