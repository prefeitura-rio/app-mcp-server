"""Tools to interact with a user's chat memories"""

from enum import Enum
from typing import List, Optional, Union

import aiohttp
from aiohttp.web_exceptions import HTTPNotFound
from pydantic import BaseModel, ValidationError

from src.config.env import RMI_API_URL
from src.utils.rmi_oauth2 import get_authorization_header, is_oauth2_configured


class MemoryType(Enum):
    base = "base"
    appended = "appended"


class MemoryRelevance(Enum):
    low = "low"
    medium = "medium"
    high = "high"


class MemoryBank(BaseModel):
    memory_name: str
    description: str
    relevance: MemoryRelevance
    memory_type: MemoryType
    value: str


async def get_memories(
    user_id: str, memory_name: Optional[str] = None
) -> Union[dict, List[dict]]:
    """Get a user's memory bank.

    Args:
        user_id (str): The user's phone number.
        memory_name (str, optional): The name of the memory bank. If None is given, return a list of all memory banks. Defaults to None.

    Returns:
        Union[dict, List[dict]]: Memory bank list or single memory bank.
    """

    # GET /v1/memory/{user_id}
    # GET /v1/memory/{user_id}/{memory_name}

    # Handle empty strings as memory name
    if isinstance(memory_name, str) and len(memory_name.strip()) == 0:
        memory_name = None

    url = "{}/v1/memory/{}".format(RMI_API_URL, user_id)
    if memory_name is not None:
        url += "/{}".format(memory_name)

    # Use OAuth2 if configured, otherwise return unauthorized error
    if not is_oauth2_configured():
        return {"status": "Error", "detail": "Unauthorized: OAuth2 not configured"}

    headers = {"Authorization": await get_authorization_header()}

    timeout = aiohttp.ClientTimeout(total=120)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers) as response:
            response.raise_for_status()
            return await response.json()


async def upsert_memory(
    user_id: str,
    memory_name: str,
    memory_bank: dict,
) -> dict:
    """Create or update a user's memory bank.

    Args:
        user_id (str): The user's phone number.
        memory_name (str): The name of the memory bank.
        memory_bank (dict): Memory bank data as a dictionary.

    Returns:
        dict: A dictionary with status of the operation.
    """

    # POST /v1/memory/{user_id}
    # PUT /v1/memory/{user_id}/{memory_name}

    try:
        validated_memory_bank = MemoryBank(**memory_bank).model_dump(mode="json")
    except ValidationError:
        return {"status": "Error", "detail": "Invalid memory bank"}

    url = "{}/v1/memory/{}".format(RMI_API_URL, user_id)
    if exists:
        url += "/{}".format(memory_name)

    # Use OAuth2 if configured, otherwise return unauthorized error
    if not is_oauth2_configured():
        return {"status": "Error", "detail": "Unauthorized: OAuth2 not configured"}

    headers = {"Authorization": await get_authorization_header()}

    timeout = aiohttp.ClientTimeout(total=120)

    # Tries to update the memory bank
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                "PUT", url, headers=headers, json=validated_memory_bank
            ) as response:
                response.raise_for_status()
                return await response.json()
    # If the memory bank does not exists, creates it
    except HTTPNotFound:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                "POST", url, headers=headers, json=validated_memory_bank
            ) as response:
                response.raise_for_status()
                return await response.json()
