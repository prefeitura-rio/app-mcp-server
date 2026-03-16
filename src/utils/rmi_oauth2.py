"""OAuth2 Client Credentials flow implementation for RMI API authentication"""

import asyncio
import time
from typing import Optional, Dict, Any
import httpx
from loguru import logger

from src.config.env import (
    RMI_OAUTH_ISSUER,
    RMI_OAUTH_CLIENT_ID,
    RMI_OAUTH_CLIENT_SECRET,
    RMI_OAUTH_SCOPES,
)
from src.utils.http_client import InterceptedHTTPClient
from src.utils.error_interceptor import interceptor


class OAuth2TokenManager:
    """Manages OAuth2 tokens for RMI API using Client Credentials flow"""
    
    def __init__(self):
        self._access_token: Optional[str] = None
        self._token_expiry: Optional[float] = None
        self._lock = asyncio.Lock()
    
    async def get_access_token(self) -> str:
        """Get a valid access token, refreshing if necessary"""
        async with self._lock:
            # Check if we have a valid token
            if self._access_token and self._token_expiry and time.time() < self._token_expiry:
                return self._access_token
            
            # Get new token
            token_data = await self._request_token()
            self._access_token = token_data["access_token"]
            # Set expiry with 5-minute buffer for safety
            self._token_expiry = time.time() + token_data["expires_in"] - 300
            return self._access_token
    
    @interceptor(source={"source": "mcp", "tool": "oauth2"})
    async def _request_token(self) -> Dict[str, Any]:
        """Request a new access token using Client Credentials flow"""
        if not all([RMI_OAUTH_ISSUER, RMI_OAUTH_CLIENT_ID, RMI_OAUTH_CLIENT_SECRET]):
            raise ValueError(
                "OAuth2 configuration incomplete. "
                "Please set RMI_OAUTH_ISSUER, RMI_OAUTH_CLIENT_ID, and RMI_OAUTH_CLIENT_SECRET environment variables."
            )

        token_url = f"{RMI_OAUTH_ISSUER}/protocol/openid-connect/token"

        data = {
            "grant_type": "client_credentials",
            "client_id": RMI_OAUTH_CLIENT_ID,
            "client_secret": RMI_OAUTH_CLIENT_SECRET,
            "scope": RMI_OAUTH_SCOPES,
        }

        try:
            async with InterceptedHTTPClient(
                user_id="system",
                source={"source": "mcp", "tool": "oauth2"},
                timeout=30.0
            ) as client:
                response = await client.post(
                    token_url,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"}
                )
                if response.status_code != 200:
                    error_text = response.text
                    logger.error(f"OAuth2 token request failed: {response.status_code} - {error_text}")
                    raise Exception(f"OAuth2 token request failed: {response.status_code} - {error_text}")

                token_data = response.json()

                if "access_token" not in token_data:
                    logger.error(f"OAuth2 response missing access_token: {token_data}")
                    raise Exception("OAuth2 response missing access_token")

                logger.info("Successfully obtained OAuth2 access token")
                return token_data

        except httpx.RequestError as e:
            logger.error(f"OAuth2 token request failed: {e}")
            raise Exception(f"OAuth2 token request failed: {e}")


# Global instance
_token_manager: Optional[OAuth2TokenManager] = None


async def get_rmi_access_token() -> str:
    """Get an access token for RMI API using OAuth2 Client Credentials flow"""
    global _token_manager
    
    if _token_manager is None:
        _token_manager = OAuth2TokenManager()
    
    return await _token_manager.get_access_token()


async def get_authorization_header() -> str:
    """Get Authorization header value with Bearer token"""
    token = await get_rmi_access_token()
    return f"Bearer {token}"


def is_oauth2_configured() -> bool:
    """Check if OAuth2 configuration is available"""
    return all([RMI_OAUTH_ISSUER, RMI_OAUTH_CLIENT_ID, RMI_OAUTH_CLIENT_SECRET])
