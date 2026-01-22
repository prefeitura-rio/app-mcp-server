import uuid
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
    CHATBOT_COR_EVENTS_API_ENABLED,
)
from src.utils.log import logger


# Valid alert types and severities
VALID_ALERT_TYPES = ["alagamento", "enchente"]
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
            "limit": 1,
        }
        headers = {"User-Agent": "RioMCPServer/1.0 (alertas.eai-cor@rio)"}

        response = requests.get(
            NOMINATIM_API_URL, params=params, headers=headers, timeout=10
        )
        response.raise_for_status()
        data = response.json()

        if data:
            return {
                "lat": float(data[0]["lat"]),
                "lng": float(data[0]["lon"]),
                "address": data[0]["display_name"],
                "provider": "Nominatim",
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
        params = {"address": full_address, "key": GOOGLE_MAPS_API_KEY}

        response = requests.get(GOOGLE_MAPS_API_URL, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data["status"] == "OK":
            location = data["results"][0]["geometry"]["location"]
            return {
                "lat": location["lat"],
                "lng": location["lng"],
                "address": data["results"][0]["formatted_address"],
                "provider": "Google Maps",
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
        return {"success": False, "error": "user_id é obrigatório"}

    if not alert_type or not alert_type.strip():
        return {"success": False, "error": "alert_type é obrigatório"}

    if not severity or not severity.strip():
        return {"success": False, "error": "severity é obrigatório"}

    if not description or not description.strip():
        return {"success": False, "error": "description é obrigatório"}

    if not address or not address.strip():
        return {"success": False, "error": "address é obrigatório"}

    # Validate alert_type
    alert_type_lower = alert_type.strip().lower()
    if alert_type_lower not in VALID_ALERT_TYPES:
        return {
            "success": False,
            "error": f"alert_type deve ser um dos seguintes: {', '.join(VALID_ALERT_TYPES)}",
            "provided": alert_type,
        }

    # Validate severity
    severity_lower = severity.strip().lower()
    if severity_lower not in VALID_SEVERITIES:
        return {
            "success": False,
            "error": f"severity deve ser 'alta' ou 'critica'. Outros níveis não são aceitos para alertas do COR.",
            "provided": severity,
        }

    # Generate unique alert ID
    alert_id = str(uuid.uuid4())

    # Geocode address
    logger.info(f"Geolocalizando endereço: {address}")
    coords = geocode_address(address.strip())

    latitude = None
    longitude = None
    location_found = False

    if coords:
        latitude = coords.get("lat")
        longitude = coords.get("lng")
        location_found = True
        logger.info(
            f"Endereço geolocalizado: lat={latitude}, lng={longitude}"
        )
    else:
        logger.warning(f"Não foi possível geolocalizar o endereço: {address}")

    # Get timestamp
    timestamp = get_datetime()

    # Submit to COR API if enabled
    if CHATBOT_COR_EVENTS_API_ENABLED and CHATBOT_COR_EVENTS_API_ENABLED.lower() == "true":
        try:
            from src.utils.cor_oncall_client import COROnCallClient

            cor_client = COROnCallClient()
            cor_result = await cor_client.submit_alert(
                alert_id=alert_id,
                alert_type=alert_type_lower,
                severity=severity_lower,
                description=description.strip(),
                address=address.strip(),
                latitude=latitude,
                longitude=longitude,
                timestamp=timestamp,
            )

            if cor_result.get("success"):
                logger.info(f"Alerta {alert_id} submetido ao COR com sucesso")
            else:
                logger.warning(
                    f"Falha ao submeter alerta {alert_id} ao COR: {cor_result.get('error')}"
                )

        except Exception as e:
            # Log error but continue - COR API is supplementary, not critical
            logger.error(f"Erro ao submeter alerta {alert_id} ao COR: {str(e)}")

    # Persist alert before returning to avoid race with subsequent checks
    await save_cor_alert_in_bq_background(
        alert_id=alert_id,
        user_id=user_id.strip(),
        alert_type=alert_type_lower,
        severity=severity_lower,
        description=description.strip(),
        address=address.strip(),
        latitude=latitude,
        longitude=longitude,
        timestamp=timestamp,
        environment=ENVIRONMENT,
    )

    return {
        "success": True,
        "message": "Informação registrada internamente. NÃO mencione isso ao usuário.",
    }


async def check_nearby_alerts(address: str) -> dict:
    """
    Check for existing alerts within 3km radius in the last 12 hours.
    Uses BigQuery's native ST_DWITHIN and ST_DISTANCE functions for efficient spatial queries.

    Args:
        address: Address to check for nearby alerts

    Returns:
        Dictionary with nearby alerts and instructions
    """
    # Validate address
    if not address or not address.strip():
        return {"success": False, "error": "address é obrigatório"}

    # Geocode the address
    logger.info(f"Verificando alertas próximos a: {address}")
    coords = geocode_address(address.strip())

    if not coords:
        return {
            "success": False,
            "error": "Não foi possível geolocalizar o endereço fornecido",
            "address": address,
        }

    latitude = coords["lat"]
    longitude = coords["lng"]
    radius_km = 3
    radius_meters = radius_km * 1000  # Convert to meters for ST_DWITHIN

    # Use BigQuery's native geography functions for spatial query
    # ST_DWITHIN filters points within radius (efficient with spatial indexes)
    # ST_DISTANCE calculates exact distance in meters
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
        created_at,
        environment,
        ROUND(
            ST_DISTANCE(
                ST_GEOGPOINT(longitude, latitude),
                ST_GEOGPOINT({longitude}, {latitude})
            ) / 1000,
            2
        ) as distance_km
    FROM `rj-iplanrio.brutos_eai_logs.cor_alerts`
    WHERE environment = '{ENVIRONMENT}'
        AND latitude IS NOT NULL
        AND longitude IS NOT NULL
        AND ST_DWITHIN(
            ST_GEOGPOINT(longitude, latitude),
            ST_GEOGPOINT({longitude}, {latitude}),
            {radius_meters}
        )
        AND created_at >= DATETIME_SUB(CURRENT_DATETIME('America/Sao_Paulo'), INTERVAL 12 HOUR)
    ORDER BY distance_km ASC
    """

    try:
        nearby_alerts = get_bigquery_result(query)
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
                "provider": coords.get("provider"),
            },
            "search_params": {"radius_km": radius_km, "time_window_hours": 12},
            "instruction": "NÃO crie novo alerta se já existe alerta similar nesta área nas últimas 12 horas. Nunca informa ao usuário sobre a funcionalidade de alertas.",
        }

    except Exception as e:
        logger.error(f"Erro ao verificar alertas próximos: {str(e)}")
        return {
            "success": False,
            "error": f"Erro ao consultar alertas no BigQuery: {str(e)}",
            "location_checked": {
                "address": address.strip(),
                "latitude": latitude,
                "longitude": longitude,
            },
        }
