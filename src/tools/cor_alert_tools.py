import asyncio
import uuid
import math
from typing import Optional
import requests

from src.utils.bigquery import (
    save_cor_alert_in_bq_background,
    get_bigquery_result,
    get_datetime,
)
from src.config.env import (
    ENVIRONMENT,
    NOMINATIM_API_URL,
    GOOGLE_MAPS_API_URL,
    GOOGLE_MAPS_API_KEY,
)
from src.utils.log import logger


# Valid alert types and severities
VALID_ALERT_TYPES = ["alagamento", "enchente", "dano_chuva"]
VALID_SEVERITIES = ["alta", "critica"]


def get_coordinates_nominatim(address: str) -> dict:
    """
    Get coordinates from Nominatim API.

    Args:
        address: Address to geocode

    Returns:
        Dictionary with lat, lng, and provider, or empty dict if failed
    """
    try:
        params = {
            "q": f"{address}, Rio de Janeiro, RJ, Brasil",
            "format": "json",
            "addressdetails": 1,
            "limit": 1
        }
        headers = {
            "User-Agent": "RioMCPServer/1.0 (alertas.cor@rio.rj.gov.br)"
        }

        response = requests.get(NOMINATIM_API_URL, params=params, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data:
            return {
                "lat": float(data[0]["lat"]),
                "lng": float(data[0]["lon"]),
                "address": data[0]["display_name"],
                "provider": "Nominatim"
            }
    except Exception as e:
        logger.warning(f"Erro ao geolocalizar com Nominatim: {str(e)}")

    return {}


def get_coordinates_google(address: str) -> dict:
    """
    Get coordinates from Google Maps API.

    Args:
        address: Address to geocode

    Returns:
        Dictionary with lat, lng, and provider, or empty dict if failed
    """
    try:
        full_address = f"{address}, Rio de Janeiro, RJ"
        params = {
            "address": full_address,
            "key": GOOGLE_MAPS_API_KEY
        }

        response = requests.get(GOOGLE_MAPS_API_URL, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data["status"] == "OK":
            location = data["results"][0]["geometry"]["location"]
            return {
                "lat": location["lat"],
                "lng": location["lng"],
                "address": data["results"][0]["formatted_address"],
                "provider": "Google Maps"
            }
    except Exception as e:
        logger.warning(f"Erro ao geolocalizar com Google Maps: {str(e)}")

    return {}


def geocode_address(address: str) -> dict:
    """
    Geocode an address using Nominatim with Google Maps fallback.

    Args:
        address: Address to geocode

    Returns:
        Dictionary with lat, lng, address, provider, or empty dict if both failed
    """
    # Try Nominatim first
    coords = get_coordinates_nominatim(address)

    # Fallback to Google Maps if Nominatim failed
    if not coords:
        coords = get_coordinates_google(address)

    return coords


def extract_bairro_from_address(address: str, coords: dict) -> Optional[str]:
    """
    Extract bairro (neighborhood) from geocoded address.

    Args:
        address: Original address
        coords: Geocoded coordinates dictionary

    Returns:
        Bairro name or None if not found
    """
    if not coords or "address" not in coords:
        return None

    full_address = coords.get("address", "")

    # Try to extract bairro from address components
    # Common patterns: "Bairro, Cidade" or "Rua X - Bairro, Cidade"
    parts = full_address.split(",")

    # Look for Rio de Janeiro reference
    for i, part in enumerate(parts):
        if "Rio de Janeiro" in part and i > 0:
            # The part before "Rio de Janeiro" is likely the bairro
            potential_bairro = parts[i - 1].strip()
            # Remove common prefixes
            potential_bairro = potential_bairro.replace("Região Geográfica Imediata de", "").strip()
            potential_bairro = potential_bairro.replace("Mesorregião Metropolitana do", "").strip()
            return potential_bairro

    return None


def calculate_distance_haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate distance between two coordinates using Haversine formula.

    Args:
        lat1: Latitude of first point
        lon1: Longitude of first point
        lat2: Latitude of second point
        lon2: Longitude of second point

    Returns:
        Distance in kilometers
    """
    # Earth radius in kilometers
    R = 6371.0

    # Convert degrees to radians
    lat1_rad = math.radians(lat1)
    lon1_rad = math.radians(lon1)
    lat2_rad = math.radians(lat2)
    lon2_rad = math.radians(lon2)

    # Haversine formula
    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad

    a = math.sin(dlat / 2)**2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    distance = R * c
    return distance


async def create_cor_alert(
    user_id: str,
    alert_type: str,
    severity: str,
    description: str,
    address: str,
) -> dict:
    """
    Create a new COR alert for flooding/heavy rain situations.

    Args:
        user_id: User ID reporting the alert
        alert_type: Type of alert (alagamento, enchente, dano_chuva)
        severity: Severity level (alta or critica)
        description: Detailed description of the problem
        address: Address where the incident is occurring

    Returns:
        Dictionary with success status and alert details
    """
    # Validate required parameters
    if not user_id or not user_id.strip():
        return {
            "success": False,
            "error": "user_id é obrigatório"
        }

    if not alert_type or not alert_type.strip():
        return {
            "success": False,
            "error": "alert_type é obrigatório"
        }

    if not severity or not severity.strip():
        return {
            "success": False,
            "error": "severity é obrigatório"
        }

    if not description or not description.strip():
        return {
            "success": False,
            "error": "description é obrigatório"
        }

    if not address or not address.strip():
        return {
            "success": False,
            "error": "address é obrigatório"
        }

    # Validate alert_type
    alert_type_lower = alert_type.strip().lower()
    if alert_type_lower not in VALID_ALERT_TYPES:
        return {
            "success": False,
            "error": f"alert_type deve ser um dos seguintes: {', '.join(VALID_ALERT_TYPES)}",
            "provided": alert_type
        }

    # Validate severity
    severity_lower = severity.strip().lower()
    if severity_lower not in VALID_SEVERITIES:
        return {
            "success": False,
            "error": f"severity deve ser 'alta' ou 'critica'. Outros níveis não são aceitos para alertas do COR.",
            "provided": severity
        }

    # Generate unique alert ID
    alert_id = str(uuid.uuid4())

    # Geocode address
    logger.info(f"Geolocalizando endereço: {address}")
    coords = geocode_address(address.strip())

    latitude = None
    longitude = None
    bairro = None
    location_found = False

    if coords:
        latitude = coords.get("lat")
        longitude = coords.get("lng")
        bairro = extract_bairro_from_address(address, coords)
        location_found = True
        logger.info(f"Endereço geolocalizado: lat={latitude}, lng={longitude}, bairro={bairro}")
    else:
        logger.warning(f"Não foi possível geolocalizar o endereço: {address}")

    # Get timestamp
    timestamp = get_datetime()

    # Save to BigQuery in background
    asyncio.create_task(
        save_cor_alert_in_bq_background(
            alert_id=alert_id,
            user_id=user_id.strip(),
            alert_type=alert_type_lower,
            severity=severity_lower,
            description=description.strip(),
            address=address.strip(),
            latitude=latitude,
            longitude=longitude,
            bairro=bairro,
            timestamp=timestamp,
            environment=ENVIRONMENT,
        )
    )

    return {
        "success": True,
        "alert_id": alert_id,
        "timestamp": timestamp,
        "location_found": location_found,
        "location": {
            "address": address.strip(),
            "latitude": latitude,
            "longitude": longitude,
            "bairro": bairro,
            "provider": coords.get("provider") if coords else None
        },
        "message": "Alerta registrado com sucesso no sistema do COR. A equipe será notificada."
    }


async def check_nearby_alerts(address: str) -> dict:
    """
    Check for existing alerts within 3km radius in the last 12 hours.

    Args:
        address: Address to check for nearby alerts

    Returns:
        Dictionary with nearby alerts and instructions
    """
    # Validate address
    if not address or not address.strip():
        return {
            "success": False,
            "error": "address é obrigatório"
        }

    # Geocode the address
    logger.info(f"Verificando alertas próximos a: {address}")
    coords = geocode_address(address.strip())

    if not coords:
        return {
            "success": False,
            "error": "Não foi possível geolocalizar o endereço fornecido",
            "address": address
        }

    latitude = coords["lat"]
    longitude = coords["lng"]

    # Calculate approximate lat/lng delta for 3km radius
    # 1 degree of latitude ≈ 111km
    # 1 degree of longitude ≈ 111km * cos(latitude)
    radius_km = 3
    delta_lat = radius_km / 111.0
    delta_lng = radius_km / (111.0 * math.cos(math.radians(latitude)))

    min_lat = latitude - delta_lat
    max_lat = latitude + delta_lat
    min_lng = longitude - delta_lng
    max_lng = longitude + delta_lng

    # Build query to find alerts in the last 12 hours within approximate radius
    query = f"""
    SELECT
        alert_id,
        user_id,
        alert_type,
        severity,
        description,
        address,
        latitude,
        longitude,
        bairro,
        created_at,
        environment
    FROM `rj-iplanrio.brutos_eai_logs.cor_alerts`
    WHERE environment = '{ENVIRONMENT}'
        AND latitude IS NOT NULL
        AND longitude IS NOT NULL
        AND latitude BETWEEN {min_lat} AND {max_lat}
        AND longitude BETWEEN {min_lng} AND {max_lng}
        AND created_at >= DATETIME_SUB(CURRENT_DATETIME('America/Sao_Paulo'), INTERVAL 12 HOUR)
    ORDER BY created_at DESC
    """

    try:
        results = get_bigquery_result(query)

        # Filter by exact distance using Haversine formula
        nearby_alerts = []
        for alert in results:
            alert_lat = alert.get("latitude")
            alert_lng = alert.get("longitude")

            if alert_lat and alert_lng:
                distance = calculate_distance_haversine(
                    latitude, longitude,
                    alert_lat, alert_lng
                )

                if distance <= radius_km:
                    alert["distance_km"] = round(distance, 2)
                    nearby_alerts.append(alert)

        # Sort by distance
        nearby_alerts.sort(key=lambda x: x["distance_km"])

        has_nearby = len(nearby_alerts) > 0

        return {
            "success": True,
            "has_nearby_alerts": has_nearby,
            "count": len(nearby_alerts),
            "alerts": nearby_alerts,
            "location_checked": {
                "address": address.strip(),
                "latitude": latitude,
                "longitude": longitude,
                "provider": coords.get("provider")
            },
            "search_params": {
                "radius_km": radius_km,
                "time_window_hours": 12
            },
            "instruction": "NÃO crie novo alerta se já existe alerta similar nesta área nas últimas 12 horas. Informe ao usuário que o alerta já foi registrado na região e forneça os detalhes dos alertas existentes."
        }

    except Exception as e:
        logger.error(f"Erro ao verificar alertas próximos: {str(e)}")
        return {
            "success": False,
            "error": f"Erro ao consultar alertas no BigQuery: {str(e)}",
            "location_checked": {
                "address": address.strip(),
                "latitude": latitude,
                "longitude": longitude
            }
        }
