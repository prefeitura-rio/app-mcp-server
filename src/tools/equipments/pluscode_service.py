import json
from typing import List, Optional
from google.cloud.bigquery.table import Row

from src.tools.equipments.utils import (
    CustomJSONEncoder,
    get_plus8_coords_from_address,
)
from src.config import env as config
from src.utils.bigquery import get_bigquery_client

# from src.utils.log import logger


def get_bigquery_result(query: str):
    bq_client = get_bigquery_client()
    query_job = bq_client.query(query)
    result = query_job.result(page_size=config.GOOGLE_BIGQUERY_PAGE_SIZE)
    data = []
    for page in result.pages:
        for row in page:
            row: Row
            row_data = dict(row.items())
            data.append(row_data)

    data_str = json.dumps(data, cls=CustomJSONEncoder, indent=2, ensure_ascii=False)

    return json.loads(data_str)


async def get_pluscode_coords_equipments(
    address, categories: Optional[List[str]] = []
) -> dict:

    plus8, coords = get_plus8_coords_from_address(address=address)
    if not coords:
        raise Exception("No coords found")

    if plus8:
        latitude = coords["lat"]
        longitude = coords["lng"]
        query = f"""
            with
            equipamentos as (
                select
                    t.plus8 as plus8_grid,
                    eq.plus8,
                    eq.plus10,
                    eq.plus11,
                    cast(eq.distancia_metros as int64) as distancia_metros,
                    t.secretaria_responsavel,
                    t.categoria,
                    eq.id_equipamento,
                    eq.nome_oficial,
                    eq.nome_popular,
                    eq.endereco.logradouro,
                    eq.endereco.numero,
                    eq.endereco.complemento,
                    coalesce(eq.bairro.bairro, eq.endereco.bairro) as bairro,
                    eq.bairro.regiao_planejamento,
                    eq.bairro.regiao_administrativa,
                    eq.bairro.subprefeitura,
                    eq.contato,
                    eq.ativo,
                    eq.aberto_ao_publico,
                    eq.horario_funcionamento,
                    eq.updated_at,
                from `rj-iplanrio.plus_codes.codes` t, unnest(equipamentos) as eq
                where eq.use = TRUE 
                and t.plus8 = "{plus8}"
                __replace_categories__
                qualify
                    row_number() over (
                        partition by t.plus8, t.secretaria_responsavel, t.categoria
                        order by cast(eq.distancia_metros as int64)
                    )
                    = 1
            ),

            tb_territorio as (
            SELECT 
                secretaria_responsavel,
                categoria,
                geometry,
                equipamentos as eq
            FROM `rj-iplanrio.plus_codes.territorio`
            ),
            
            equipamentos_territorio as (
                SELECT 
                    CAST(NULL as STRING) as plus8_grid,
                    eq.plus8,
                    eq.plus10,
                    eq.plus11,
                    CAST(st_distance(ST_GEOGPOINT(eq.longitude,eq.latitude), ST_GEOGPOINT({longitude}, {latitude})) AS INT64) as distancia_metros,                    t.secretaria_responsavel,
                    t.categoria,
                    eq.id_equipamento,
                    eq.nome_oficial,
                    eq.nome_popular,
                    eq.endereco.logradouro,
                    eq.endereco.numero,
                    eq.endereco.complemento,
                    coalesce(eq.bairro.bairro, eq.endereco.bairro) as bairro,
                    eq.bairro.regiao_planejamento,
                    eq.bairro.regiao_administrativa,
                    eq.bairro.subprefeitura,
                    eq.contato,
                    eq.ativo,
                    eq.aberto_ao_publico,
                    eq.horario_funcionamento,
                    eq.updated_at,
                FROM tb_territorio t
                where eq.use = TRUE 
                and ST_WITHIN(ST_GEOGPOINT({longitude}, {latitude}), geometry)
                __replace_categories__
                order by eq.secretaria_responsavel, eq.categoria
            ),
            
           final_tb as (
                select *
                from equipamentos eq
                UNION ALL
                SELECT * 
                FROM equipamentos_territorio
            )

            SELECT *
            FROM final_tb
            order by secretaria_responsavel, categoria
        """

    if categories:
        # logger.info(f"Categories: {categories}")

        # If either "CF" or "CMS" in categories, ensure all 3 are included
        # target_categories = ["CF", "CMS", "EQUIPE DA FAMILIA"]
        # if any(cat in categories for cat in target_categories):
        #     required_categories = ["CF", "CMS", "EQUIPE DA FAMILIA"]
        #     for cat in required_categories:
        #         if cat not in categories:
        #             categories.append(cat)

        categorias_filter = "and t.categoria in ("
        for i in range(len(categories)):
            if i != len(categories) - 1:
                categorias_filter += f"'{categories[i]}', "
            else:
                categorias_filter += f"'{categories[i]}'"

        categorias_filter += ")"
        query = query.replace("__replace_categories__", categorias_filter)
    else:
        # logger.info("No categories provided. Returning all categories.")
        query = query.replace("__replace_categories__", "")

    try:
        # print(query)
        data = get_bigquery_result(query=query)

        return {
            "inputs": {
                "address": address,
                "categories": categories,
            },
            "coords": coords,
            "plus8": plus8,
            "data": data,
        }
    except Exception as e:
        # logger.error(f"Erro no request do bigquery: {e}")
        return {
            "error": "Erro no request do bigquery",
            "message": str(e),
        }


async def get_category_equipments() -> dict:
    query = f"""
        with
        equipamentos as (
            SELECT
                DISTINCT
                    TRIM(t.secretaria_responsavel) as secretaria_responsavel,
                    TRIM(t.categoria) as categoria
            FROM `rj-iplanrio.plus_codes.codes` t, unnest(equipamentos) as eq
            WHERE t.categoria IS NOT NULL and eq.use = TRUE
            UNION ALL
            SELECT 
            DISTINCT
                    TRIM(t.secretaria_responsavel) as secretaria_responsavel,
                    TRIM(t.categoria) as categoria
            FROM `rj-iplanrio.plus_codes.territorio` t
            WHERE t.categoria IS NOT NULL and equipamentos.use = TRUE
        )

    select *
    from equipamentos eq
    order by eq.secretaria_responsavel, eq.categoria
    """

    data = get_bigquery_result(query=query)
    categories = {}
    for d in data:

        if d["secretaria_responsavel"] not in categories:
            categories[d["secretaria_responsavel"]] = []
        categories[d["secretaria_responsavel"]].append(d["categoria"])

    return categories


async def get_tematic_instructions_for_equipments(tema: str = "geral") -> List[dict]:
    where_clause = f"WHERE tema = '{tema}'" if tema != "geral" else ""
    query = f"""
        SELECT 
            * 
        FROM `rj-iplanrio.plus_codes.equipamentos_instrucoes`
        {where_clause}
    """
    data = get_bigquery_result(query=query)
    return data


# if __name__ == "__main__":
# import asyncio

# cat = asyncio.run(get_category_equipments())
# data = asyncio.run(get_pluscode_equipments(address="Avenida Presidente Vargas, 1"))

# print(cat)
# print(data)
