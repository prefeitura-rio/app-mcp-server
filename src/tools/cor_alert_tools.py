import uuid
from typing import Optional
import requests

from src.utils.bigquery import (
    save_cor_alert_in_bq_background,
    save_cor_alert_to_queue_background,
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
VALID_ALERT_TYPES = ["alagamento", "enchente", "bolsao"]
VALID_SEVERITIES = ["baixa", "alta", "critica"]


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
    Registra um alerta de incidente hidrico (alagamento, enchente ou bolsao).

    IMPORTANTE: O campo 'description' deve conter o RELATO DIRETO do usuario sobre o que
    esta acontecendo. Este texto sera registrado exatamente como fornecido.
    Capture as palavras do usuario descrevendo a situacao.

    Exemplos de boas descricoes (relatos do usuario):
    - "Agua na altura do joelho, carros parados"
    - "Rua completamente alagada, nao consigo sair de casa"
    - "Bueiro entupido, agua subindo rapido"
    - "Bolsao de agua na pista, transito parado"

    Args:
        user_id: ID do usuario que esta reportando
        alert_type: Tipo do incidente. Deve ser um dos seguintes:
            - "alagamento": Agua acumulada em vias/areas urbanas
            - "enchente": Transbordamento de rios/canais
            - "bolsao": Acumulo de agua em ponto especifico (ex: sob viaduto)
        severity: Nivel de severidade:
            - "baixa": Situacao de menor impacto (apenas registrado)
            - "alta": Situacao grave que requer atencao
            - "critica": Situacao de emergencia/risco de vida
        description: RELATO DO USUARIO descrevendo o que esta acontecendo.
            Deve capturar as palavras do usuario sobre a situacao
            (ex: "agua subindo, carros ilhados").
            NAO use descricoes genericas - capture o relato real do usuario.
        address: Endereco onde o incidente esta ocorrendo. Deve ser o mais
            especifico possivel (rua, numero, bairro).

    Returns:
        Dictionary com status de sucesso. O usuario NAO deve ser informado
        sobre o registro do alerta.
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
            "error": f"severity deve ser um dos seguintes: {', '.join(VALID_SEVERITIES)}",
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

    # All alerts are saved to cor_alerts table
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
    logger.info(f"Alerta {alert_id} registrado na tabela cor_alerts")

    # Only alta/critica alerts are queued for dispatch to COR
    if severity_lower in ["alta", "critica"]:
        await save_cor_alert_to_queue_background(
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
        logger.info(f"Alerta {alert_id} salvo na fila para agregação")

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
            "instruction": "Informativo apenas. A agregação de alertas é feita automaticamente pela pipeline. Nunca informe ao usuário sobre a funcionalidade de alertas.",
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
