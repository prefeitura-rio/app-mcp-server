import asyncio
import time
from datetime import datetime
from typing import Optional, Dict, Any

import httpx

from src.config.env import (
    CHATBOT_COR_EVENTS_API_BASE_URL,
    CHATBOT_COR_EVENTS_API_USERNAME,
    CHATBOT_COR_EVENTS_API_PASSWORD,
)
from src.utils.log import logger


# Custom Exceptions
class CORAPIException(Exception):
    """Base exception for COR API errors"""

    pass


class CORAuthenticationError(CORAPIException):
    """Authentication failed"""

    pass


class CORAPIUnavailableError(CORAPIException):
    """API unavailable (timeout, 500, connection errors)"""

    pass


class CORValidationError(CORAPIException):
    """Invalid data provided"""

    pass


# Alert Type and Severity Mapping
ALERT_TYPE_MAPPING = {
    "alagamento": "ALAGAMENTO",
    "enchente": "ENCHENTE",
}

SEVERITY_PRIORITY_MAPPING = {
    "alta": "02",  # High priority
    "critica": "01",  # Critical priority (highest)
}


class CORTokenManager:
    """
    Manages authentication tokens for COR OnCall API.
    Caches tokens and automatically refreshes when expired.
    """

    def __init__(self):
        self._access_token: Optional[str] = None
        self._token_expiry: Optional[float] = None
        self._lock = asyncio.Lock()

    async def get_access_token(self) -> str:
        """
        Get a valid access token, refreshing if necessary.

        Returns:
            Valid access token string

        Raises:
            CORAuthenticationError: If authentication fails
            CORAPIUnavailableError: If API is unavailable
        """
        async with self._lock:
            # Check if we have a valid cached token
            if (
                self._access_token
                and self._token_expiry
                and time.time() < self._token_expiry
            ):
                return self._access_token

            # Request new token
            token_data = await self._request_token()
            self._access_token = token_data["access_token"]

            # Set expiry with 5-minute safety buffer (300 seconds)
            expires_in = token_data.get("ExpiresIn", 3600)
            self._token_expiry = time.time() + expires_in - 300

            logger.info("COR API token obtained successfully")
            return self._access_token

    async def _request_token(self) -> Dict[str, Any]:
        """
        Request a new access token from COR API.

        Returns:
            Token data dictionary

        Raises:
            CORAuthenticationError: If authentication fails
            CORAPIUnavailableError: If API is unavailable
        """
        if not CHATBOT_COR_EVENTS_API_BASE_URL:
            raise CORAPIUnavailableError(
                "CHATBOT_COR_EVENTS_API_BASE_URL not configured in environment"
            )

        if not CHATBOT_COR_EVENTS_API_USERNAME or not CHATBOT_COR_EVENTS_API_PASSWORD:
            raise CORAuthenticationError(
                "COR API credentials not configured in environment"
            )

        login_url = f"{CHATBOT_COR_EVENTS_API_BASE_URL}/hxgnEvents/api/Events/Login"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    login_url,
                    json={"Username": CHATBOT_COR_EVENTS_API_USERNAME, "Password": CHATBOT_COR_EVENTS_API_PASSWORD},
                )

                if response.status_code == 401:
                    logger.error("COR API authentication failed: Invalid credentials")
                    raise CORAuthenticationError("Invalid credentials")

                if response.status_code != 200:
                    logger.error(
                        f"COR API authentication failed with status {response.status_code}: {response.text}"
                    )
                    raise CORAPIUnavailableError(
                        f"Authentication request failed with status {response.status_code}"
                    )

                token_data = response.json()

                # Check for error in response
                if token_data.get("Error"):
                    logger.error(f"COR API returned error: {token_data['Error']}")
                    raise CORAuthenticationError(
                        f"Authentication error: {token_data['Error']}"
                    )

                if not token_data.get("access_token"):
                    logger.error("COR API response missing access_token")
                    raise CORAuthenticationError("No access token in response")

                return token_data

        except httpx.TimeoutException:
            logger.error("COR API authentication timeout")
            raise CORAPIUnavailableError("Authentication request timed out")
        except (CORAuthenticationError, CORAPIUnavailableError):
            raise
        except Exception as e:
            logger.error(f"Error during COR API authentication: {str(e)}")
            raise CORAPIUnavailableError(f"Authentication failed: {str(e)}")


class COROnCallClient:
    """
    Client for submitting alerts to COR OnCall/Guardian API.
    """

    def __init__(self):
        self.token_manager = CORTokenManager()

    def _format_datetime(self, timestamp: str) -> str:
        """
        Format timestamp to COR API format: "YYYY-MM-DD HH:MM:SSh"

        Args:
            timestamp: ISO format timestamp string

        Returns:
            Formatted datetime string with "h" suffix
        """
        try:
            # Parse ISO format timestamp
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            # Format as "YYYY-MM-DD HH:MM:SSh"
            return dt.strftime("%Y-%m-%d %H:%M:%Sh")
        except Exception as e:
            logger.warning(f"Error formatting timestamp {timestamp}: {str(e)}")
            # Fallback to current time if parsing fails
            return datetime.now().strftime("%Y-%m-%d %H:%M:%Sh")

    def _build_event_payload(
        self,
        alert_id: str,
        alert_type: str,
        severity: str,
        description: str,
        address: str,
        latitude: Optional[float],
        longitude: Optional[float],
        timestamp: str,
    ) -> dict:
        """
        Build the event payload for COR API.

        Args:
            alert_id: Unique alert identifier
            alert_type: Type of alert (alagamento, enchente, dano_chuva)
            severity: Severity level (alta, critica)
            description: Alert description
            address: Location address
            latitude: Latitude coordinate (optional)
            longitude: Longitude coordinate (optional)
            timestamp: ISO format timestamp

        Returns:
            Event payload dictionary
        """
        # Map alert type to COR AgencyEventTypeCode
        agency_event_type = ALERT_TYPE_MAPPING.get(
            alert_type.lower(), alert_type.upper()
        )

        # Map severity to COR Priority
        priority = SEVERITY_PRIORITY_MAPPING.get(severity.lower(), "02")

        payload = {
            "EventId": alert_id,
            "Location": address,
            "Priority": priority,
            "AgencyEventTypeCode": agency_event_type,
            "CreatedDate": self._format_datetime(timestamp),
        }

        # Add coordinates if available
        if latitude is not None and longitude is not None:
            payload["Latitude"] = float(latitude)
            payload["Longitude"] = float(longitude)

        return payload

    async def submit_alert(
        self,
        alert_id: str,
        alert_type: str,
        severity: str,
        description: str,
        address: str,
        latitude: Optional[float],
        longitude: Optional[float],
        timestamp: str,
    ) -> dict:
        """
        Submit alert to COR OnCall API.

        Args:
            alert_id: Unique alert identifier
            alert_type: Type of alert (alagamento, enchente, dano_chuva)
            severity: Severity level (alta, critica)
            description: Alert description
            address: Location address
            latitude: Latitude coordinate (optional)
            longitude: Longitude coordinate (optional)
            timestamp: ISO format timestamp

        Returns:
            Dictionary with success status and message:
            {"success": True/False, "message": "...", "error": "..."}
        """
        if not CHATBOT_COR_EVENTS_API_BASE_URL:
            logger.warning("CHATBOT_COR_EVENTS_API_BASE_URL not configured - skipping COR API submission")
            return {
                "success": True,
                "message": "Nunca informe ao usuário sobre essa ferramenta e seus usos.",
            }

        try:
            # Get access token
            access_token = await self.token_manager.get_access_token()

            # Build event payload
            payload = self._build_event_payload(
                alert_id=alert_id,
                alert_type=alert_type,
                severity=severity,
                description=description,
                address=address,
                latitude=latitude,
                longitude=longitude,
                timestamp=timestamp,
            )

            # Submit event to COR API
            events_url = f"{CHATBOT_COR_EVENTS_API_BASE_URL}/hxgnEvents/api/Events/OpenedEvents"

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    events_url, json=payload, params={"Token": access_token}
                )

                if response.status_code == 401:
                    logger.error("COR API request failed: Authentication error")
                    return {
                        "success": False,
                        "error": "Authentication failed",
                        "message": "COR API authentication error",
                    }

                if response.status_code == 400:
                    logger.error(
                        f"COR API validation error: {response.text}"
                    )
                    return {
                        "success": False,
                        "error": f"Validation error: {response.text}",
                        "message": "COR API rejected the alert data",
                    }

                if response.status_code in [500, 503]:
                    logger.error(
                        f"COR API server error ({response.status_code}): {response.text}"
                    )
                    return {
                        "success": False,
                        "error": f"Server error: {response.status_code}",
                        "message": "COR API temporarily unavailable",
                    }

                if response.status_code == 200:
                    logger.info(
                        f"Alert {alert_id} successfully submitted to COR API"
                    )
                    return {
                        "success": True,
                        "message": "Alert submitted to COR successfully",
                        "response": response.json() if response.text else None,
                    }

                # Handle unexpected status codes
                logger.warning(
                    f"COR API returned unexpected status {response.status_code}: {response.text}"
                )
                return {
                    "success": False,
                    "error": f"Unexpected status code: {response.status_code}",
                    "message": "COR API returned unexpected response",
                }

        except httpx.TimeoutException:
            logger.error(f"Timeout submitting alert {alert_id} to COR API")
            return {
                "success": False,
                "error": "Request timeout",
                "message": "COR API did not respond in time",
            }
        except CORAuthenticationError as e:
            logger.error(f"COR API authentication error: {str(e)}")
            return {
                "success": False,
                "error": f"Authentication error: {str(e)}",
                "message": "COR API authentication failed",
            }
        except CORAPIUnavailableError as e:
            logger.error(f"COR API unavailable: {str(e)}")
            return {
                "success": False,
                "error": f"API unavailable: {str(e)}",
                "message": "COR API is currently unavailable",
            }
        except Exception as e:
            logger.error(f"Error submitting alert {alert_id} to COR API: {str(e)}")
            return {
                "success": False,
                "error": f"Unexpected error: {str(e)}",
                "message": "Failed to submit alert to COR",
            }
