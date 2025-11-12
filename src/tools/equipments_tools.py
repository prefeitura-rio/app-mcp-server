import asyncio
from asyncio.log import logger
from typing import Optional, List
from src.tools.equipments.pluscode_service import (
    get_category_equipments,
    get_tematic_instructions_for_equipments,
    get_pluscode_coords_equipments,
)
from src.utils.bigquery import save_response_in_bq_background
from src.config.env import EQUIPMENTS_VALID_THEMES


def get_valid_themes() -> List[str]:
    """
    Retorna a lista de temas válidos para equipamentos

    Returns:
        Lista de temas válidos configurados via variável de ambiente
    """
    return EQUIPMENTS_VALID_THEMES


def get_instructions_for_equipments(equipments_data: List[dict]) -> str:
    """
    Retorna instruções específicas baseadas nos dados dos equipamentos retornados.
    Analisa múltiplos campos (categoria, esfera, etc.) para determinar instruções apropriadas.

    Args:
        equipments_data: Lista de equipamentos com seus dados completos

    Returns:
        String com instruções específicas baseadas nos critérios encontrados
    """
    if not equipments_data or not isinstance(equipments_data, list):
        return "Retorne todos os equipamentos referente a busca do usuario, acompanhado de todas as informacoes disponiveis sobre o equipamento"
    
    instructions_parts = []
    
    # Verificar se há equipamentos de saúde (CF ou CMS)
    health_categories = ["CF", "CMS"]
    has_health_equipment = any(
        eq.get("categoria") in health_categories 
        for eq in equipments_data
    )
    
    if has_health_equipment:
        instructions_parts.append("""- Ao apresentar uma unidade de Atenção Primária (CF ou CMS), siga este formato OBRIGATORIAMENTE:
        1.  **Apresente a equipe de forma personalizada**: Chame-a de "**a sua equipe de saúde da família**" e informe o nome dela.
        2.  **Forneça APENAS o contato da equipe**: Informe o número de telefone da equipe, deixando claro que o contato é via **WhatsApp**.
        3.  **NÃO INFORME** o telefone geral da unidade (CF/CMS) para não confundir o cidadão. Informe apenas se a equipe da família não tiver telefone.
        4.  Não cite que a unidade é a **mais próxima** ou a **mais indicada**. Apenas informe que é a unidade que atende a região do cidadão.
        5.  **Explique o papel da equipe**: De forma sucinta, diga que é a equipe responsável por cuidar da saúde dele e de sua família.
        6.  **Exemplo de como estruturar a resposta**: 
            "A unidade de saúde que atende a sua região é:
                - **[Nome da CF/CMS]**
                - **Endereço:** [Endereço da CF/CMS]
                - **Distância:** [Distância da CF/CMS]
                - **Horário de funcionamento:** [Horário de Funcionamento da CF/CMS] 
            Lá, **a sua equipe de saúde da família**, chamada **[Nome da Equipe]**, é a responsável por cuidar de você e da sua família. Se precisar entrar em contato, o **WhatsApp** da sua equipe é [Número do WhatsApp da Equipe]."
        7. Caso a distância seja maior ou igual a 1000 metros, informar com a distância em quilômetros ao invés de metros. Formatar número para ter apenas uma casa decimal.""")
    
    # Verificar se há equipamentos estaduais
    has_estadual = any(
        "ESTADUAL" in str(eq.get("esfera", "")).upper()
        for eq in equipments_data
    )
    
    if has_estadual:
        instructions_parts.append("""- Para equipamentos de esfera ESTADUAL:
        1. Informe claramente que o equipamento é de responsabilidade do **Governo do Estado do Rio de Janeiro**.
        2. Explique que a prefeitura não tem gestão sobre este equipamento e que os dados podem estar desatualizados.""")
    
    # Se houver instruções específicas, retorná-las combinadas
    if instructions_parts:
        return "\n\n".join(instructions_parts)
    
    # Instruções padrão caso não haja critérios específicos
    return "Retorne todos os equipamentos referente a busca do usuario, acompanhado de todas as informacoes disponiveis sobre o equipamento"


async def get_equipments_with_instructions(
    address: str, categories: Optional[List[str]] = []
) -> dict:
    """
    Obtém equipamentos por endereço e retorna com instruções apropriadas.

    Args:
        address: Endereço para busca
        categories: Lista de categorias para filtrar

    Returns:
        Dict com equipamentos e instruções específicas
    """
    # Buscar equipamentos
    equipments_data = await get_equipments(address=address, categories=categories)

    # Verificar se há erro
    if (
        isinstance(equipments_data, list)
        and len(equipments_data) > 0
        and "error" in equipments_data[0]
    ):
        return {"error": equipments_data}

    # Obter instruções baseadas nos dados reais dos equipamentos
    instructions = get_instructions_for_equipments(equipments_data=equipments_data)

    return {
        "instructions": instructions,
        "equipamentos": equipments_data,
    }


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

    atencao_primaria_categories = ["CF", "CMS", "EQUIPE DA FAMILIA"]
    assistencia_social_categories = ["CRAS"]

    if categories and any(cat in atencao_primaria_categories for cat in categories):
        # Garantir que apenas categorias válidas sejam usadas
        categories += atencao_primaria_categories
    elif categories and any(cat in assistencia_social_categories for cat in categories):
        categories += assistencia_social_categories

    if categories:
        categories = list(set(categories))

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


async def get_equipments_instructions(tema: str = "geral") -> List[dict]:
    # Validar se o tema é válido
    if tema not in EQUIPMENTS_VALID_THEMES:
        error_response = {
            "error": "Tema inválido",
            "message": f"O tema '{tema}' não é válido. Temas válidos: {', '.join(EQUIPMENTS_VALID_THEMES)}",
            "valid_themes": EQUIPMENTS_VALID_THEMES,
            "fallback_action": "Utilizando tema 'geral' como fallback",
        }

        # Usar 'geral' como fallback
        tema = "geral"

        # Obter as instruções com o tema fallback
        response = await get_tematic_instructions_for_equipments(tema=tema)

        # Adicionar informações de erro ao response
        if isinstance(response, list):
            response.insert(0, error_response)
        else:
            response = [error_response, response]

        asyncio.create_task(
            save_response_in_bq_background(
                data=response,
                endpoint="/tools/equipments_instructions",
                dataset_id="brutos_eai_logs",
                table_id="mcp",
            )
        )
        return response

    # Se o tema é válido, proceder normalmente
    response = await get_tematic_instructions_for_equipments(tema=tema)
    asyncio.create_task(
        save_response_in_bq_background(
            data=response,
            endpoint="/tools/equipments_instructions",
            dataset_id="brutos_eai_logs",
            table_id="mcp",
        )
    )
    return response
