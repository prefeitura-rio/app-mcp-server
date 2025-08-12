import asyncio
from typing import Optional, List
from src.tools.equipments.pluscode_service import (
    get_category_equipments,
    get_tematic_instructions_for_equipments,
    get_pluscode_coords_equipments,
)
from src.utils.bigquery import save_response_in_bq_background


async def get_equipments_categories() -> dict:
    response = await get_category_equipments()
    asyncio.create_task(
        save_response_in_bq_background(
            data=response,
            endpoint="/tools/equipments_categories",
            dataset_id="brutos_eai_logs",
            table_id="mcp",
        )
    )
    return response


async def get_equipments(
    address: str, categories: Optional[List[str]] = []
) -> List[dict]:
    response = await get_pluscode_coords_equipments(
        address=address, categories=categories
    )
    asyncio.create_task(
        save_response_in_bq_background(
            data=response,
            endpoint="/tools/equipments",
            dataset_id="brutos_eai_logs",
            table_id="mcp",
        )
    )

    if response.get("data", None):
        return response["data"]
    else:
        return [
            {
                "error": "Nenhum equipamento encontrado",
                "message": "Sempre utilize a tool `equipments_instructions` antes de chamar a tool `equipments_by_address`. Assim, você poderá conferir instruções sobre os equipamentos disponíveis, regras de uso e categorias permitidas.",
            }
        ]


async def get_equipments_instructions() -> List[dict]:
    response = await get_tematic_instructions_for_equipments()
    asyncio.create_task(
        save_response_in_bq_background(
            data=response,
            endpoint="/tools/equipments_instructions",
            dataset_id="brutos_eai_logs",
            table_id="mcp",
        )
    )
    return response
