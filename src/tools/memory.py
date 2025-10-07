"""Tools to interact with a user's chat memories"""

from enum import Enum
from typing import List, Optional, Union

import aiohttp
from pydantic import BaseModel, ValidationError

from src.config.env import RMI_API_URL, RMI_API_KEY


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
    phone_number: str, memory_name: Optional[str] = None
) -> Union[dict, List[dict]]:
    """Get a user's memory bank.

    Args:
        phone_number (str): The user's phone number.
        memory_name (str, optional): The name of the memory bank. If None is given, return a list of all memory banks. Defaults to None.

    Returns:
        Union[dict, List[dict]]: Memory bank list or single memory bank.
    """

    # GET /v1/memory/{phone_number}
    # GET /v1/memory/{phone_number}/{memory_name}

    url = "{}/v1/memory/{}".format(RMI_API_URL, phone_number)
    if memory_name is not None:
        url += "/{}".format(memory_name)
    headers = {"Authorization": "Bearer {}".format(RMI_API_KEY)}

    timeout = aiohttp.ClientTimeout(total=120)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers) as response:
            response.raise_for_status()
            return await response.json()


async def upsert_memory(
    phone_number: str,
    memory_name: str,
    memory_bank: MemoryBank,
    exists: Optional[bool] = True,
) -> dict:
    """Create or update a user's memory bank.

    Args:
        phone_number (str): The user's phone number.
        memory_name (str): The name of the memory bank.
        memory_bank (MemoryBank): Memory bank data.
        exists (bool, optional): Whether the memory bank already exists. Defaults to True.

    Returns:
        dict: A dictionary with status of the operation.
    """

    # POST /v1/memory/{phone_number}
    # PUT /v1/memory/{phone_number}/{memory_name}

    if exists:
        method = "PUT"
    else:
        method = "POST"

    try:
        validated_memory_bank = MemoryBank(**memory_bank).model_dump(mode="json")
    except ValidationError:
        return {"status": "Error", "detail": "Invalid memory bank"}

    url = "{}/v1/memory/{}".format(RMI_API_URL, phone_number)
    if exists:
        url += "/{}".format(memory_name)
    headers = {"Authorization": "Bearer {}".format(RMI_API_KEY)}

    timeout = aiohttp.ClientTimeout(total=120)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(
            method, url, headers=headers, body=validated_memory_bank
        ) as response:
            response.raise_for_status()
            return await response.json()
