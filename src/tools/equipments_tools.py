import asyncio
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


def get_instructions_for_categories(categories: Optional[List[str]]) -> str:
    """
    Retorna instruções específicas baseadas nas categorias de equipamentos.

    Args:
        categories: Lista de categorias de equipamentos

    Returns:
        String com instruções específicas para as categorias
    """
    # Categorias de pontos de apoio da Defesa Civil
    apoio_categories = ["PONTOS_DE_APOIO"]

    # Categorias de saúde que requerem instruções específicas
    health_categories = ["CF", "CMS"]

    if categories and any(cat in categories for cat in health_categories):
        return """- Ao apresentar uma unidade de Atenção Primária (CF ou CMS), siga este formato OBRIGATORIAMENTE:
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
        7. Caso a distância seja maior ou igual a 1000 metros, informar com a distância em quilômetros ao invés de metros. Formatar número para ter apenas uma casa decimal.
        """

    if categories and any(cat in categories for cat in apoio_categories):
        return """- Ao apresentar um Ponto de Apoio da Defesa Civil, siga este formato OBRIGATORIAMENTE:
        1.  **Contextualize o que é um Ponto de Apoio**: Explique de forma breve que são locais preparados pela Defesa Civil para acolhimento temporário da população em situações de emergência, como enchentes, deslizamentos ou outras situações de risco.
        2.  **Apresente o equipamento mais próximo** com as seguintes informações:
            - **Nome do equipamento**
            - **Endereço completo** (logradouro, número, bairro)
            - **Distância**: Caso a distância seja maior ou igual a 1000 metros, informar em quilômetros (com 1 casa decimal). Caso contrário, informar em metros.
        3.  **Quando procurar um Ponto de Apoio**: Oriente que estes locais devem ser procurados em situações como:
            - Enchentes ou alagamentos que impossibilitem permanência em casa
            - Deslizamentos de terra ou risco iminente
            - Situações de risco estrutural na residência
            - Outras emergências orientadas pela Defesa Civil
        4.  **Contato de Emergência da Defesa Civil**: SEMPRE informe ao final que em caso de emergência, o cidadão pode ligar para a **Defesa Civil no 199** (funciona 24 horas).
        5.  **Exemplo de como estruturar a resposta**:
            "O ponto de apoio mais próximo do seu endereço é:
                - **[Nome do Equipamento]**
                - **Endereço:** [Endereço Completo]
                - **Distância:** [X metros ou X,X km]
                - **Horário de funcionamento:** [Horário, se disponível]

            Os pontos de apoio são locais preparados pela Defesa Civil para acolhimento temporário em situações de emergência, como enchentes, deslizamentos ou outras situações de risco.

            **Em caso de emergência, ligue para a Defesa Civil: 199 (funciona 24 horas)**"
        6. Caso o cidadão esteja em uma situação de emergência iminente (risco de vida, desabamento, afogamento, etc.), oriente PRIMEIRO a ligar para o 199 antes de se deslocar.
        """
    # Instruções padrão para outras categorias
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

    # Obter instruções baseadas nas categorias
    instructions = get_instructions_for_categories(categories=categories)

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

    if categories and any(cat in atencao_primaria_categories for cat in categories):
        # Garantir que apenas categorias válidas sejam usadas
        categories += atencao_primaria_categories

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
