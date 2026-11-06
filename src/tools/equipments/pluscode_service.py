import asyncio
import functools
from typing import List, Optional

from google.api_core.exceptions import GoogleAPIError
from google.cloud import bigquery

from src.tools.equipments.utils import get_plus8_coords_from_address
from src.utils.bigquery import (
    BigQueryQueryError,
    BigQueryTimeoutError,
    get_bigquery_result,
)
from src.utils.error_interceptor import interceptor
from src.utils.log import logger


class GeocodingError(Exception):
    """O endereço não pôde ser convertido em coordenadas + plus8.

    Subclasse de `Exception` para não mudar o contrato de quem já captura
    largo, mas com tipo próprio para que o chamador consiga distinguir
    "endereço não geolocalizado" de uma falha do BigQuery.
    """


async def _geocodificar(address: str):
    """Roda a geocodificação, que é síncrona e de rede, fora do event loop.

    `get_plus8_coords_from_address` faz um HTTP **bloqueante** (o
    `InterceptedHTTPClient` é construído com `sync=True` e `timeout=10.0`).
    Chamada direto de dentro de uma corrotina, ela congelava o loop inteiro
    pela duração da resposta do Google Maps — medido: 1s de geocodificação,
    1,02s de loop parado, com todas as outras requisições do pod paradas
    junto. É o mesmo defeito que o CHATR-125 corrigiu na leitura do BigQuery,
    na mesma função, só que na metade que ninguém tinha olhado — e esta roda
    em toda chamada, inclusive nas que depois acertam o cache.

    Vai para o executor default (e não para o pool de leitura do BigQuery):
    são recursos diferentes, e misturá-los faria uma geocodificação lenta
    consumir vaga de query. O teto de tempo continua sendo o `timeout=10.0`
    do próprio cliente HTTP.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, functools.partial(get_plus8_coords_from_address, address=address)
    )


@interceptor(source={"source": "mcp", "tool": "equipments"})
async def get_pluscode_coords_equipments(
    address, categories: Optional[List[str]] = None
) -> dict:
    categories = categories or []
    plus8, coords = await _geocodificar(address)
    if not coords:
        raise GeocodingError("No coords found")

    # `plus8` vazio com `coords` preenchido deixava `query`, `latitude` e
    # `longitude` sem valor e a função estourava `UnboundLocalError` na
    # montagem dos parâmetros — fora do `try`, portanto sem virar o dict de
    # erro que a tool espera. O guarda original (`if plus8:`) admitia que o
    # caso existia, mas não tratava o ramo negativo.
    if not plus8:
        raise GeocodingError(f"No plus8 code for address: {address}")

    latitude = coords["lat"]
    longitude = coords["lng"]
    query = """
            with
            equipamentos as (
                select
                    t.plus8 as plus8_grid,
                    eq.plus8,
                    eq.plus10,
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
                    eq.esfera,
                    eq.horario_funcionamento,
                    eq.updated_at,
                from `rj-iplanrio.plus_codes.codes` t, unnest(equipamentos) as eq
                where eq.use = TRUE 
                and t.plus8 = @plus8
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
                    CAST(st_distance(ST_GEOGPOINT(eq.longitude,eq.latitude), ST_GEOGPOINT(@longitude, @latitude)) AS INT64) as distancia_metros,
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
                    eq.esfera,
                    eq.horario_funcionamento,
                    eq.updated_at,
                FROM tb_territorio t
                where eq.use = TRUE 
                and ST_WITHIN(ST_GEOGPOINT(@longitude, @latitude), geometry)
                __replace_categories__
                order by eq.secretaria_responsavel, eq.categoria
            ),
            
           final_tb as (
                -- Prioridade para equipamentos do territorio
                SELECT * 
                FROM equipamentos_territorio
                UNION ALL
                -- Adiciona equipamentos da grid apenas se a categoria não existe no territorio
                SELECT *
                FROM equipamentos eq
                WHERE NOT EXISTS (
                    SELECT 1 
                    FROM equipamentos_territorio et
                    WHERE et.secretaria_responsavel = eq.secretaria_responsavel
                    AND et.categoria = eq.categoria
                )
            )

            SELECT *
            FROM final_tb
            order by secretaria_responsavel, categoria
        """

    query_parameters = [
        bigquery.ScalarQueryParameter("plus8", "STRING", plus8),
        bigquery.ScalarQueryParameter("longitude", "FLOAT64", float(longitude)),
        bigquery.ScalarQueryParameter("latitude", "FLOAT64", float(latitude)),
    ]

    if categories:
        categorias_filter = "and t.categoria in UNNEST(@categories)"
        query = query.replace("__replace_categories__", categorias_filter)
        query_parameters.append(
            bigquery.ArrayQueryParameter("categories", "STRING", categories)
        )
    else:
        query = query.replace("__replace_categories__", "")

    try:
        # Chave de cache por plus8 + categorias, e não pelas coordenadas exatas
        # (CHATR-115). A célula plus8 tem ~278m x 256m, então endereços distintos
        # dentro dela compartilham a mesma entrada — é justamente o que dá volume
        # de acerto, já que muita gente consulta a mesma região.
        #
        # O preço, decidido conscientemente: a query usa lat/lng exatos em
        # `ST_WITHIN` (quais territórios contêm o ponto) e em `st_distance`
        # (`distancia_metros`). Quem for atendido pelo cache recebe os valores
        # calculados a partir do ponto de quem populou a entrada — distância com
        # erro de até ~275m e, junto a uma fronteira de território, possivelmente
        # o conjunto da célula vizinha. Trocar por chave com coordenadas
        # arredondadas é o caminho se isso passar a incomodar.
        data = await get_bigquery_result(
            query=query,
            query_parameters=query_parameters,
            cache_namespace="equipments",
            cache_key_parts={"plus8": plus8, "cats": categories},
        )

        return {
            "inputs": {
                "address": address,
                "categories": categories,
            },
            "coords": coords,
            "plus8": plus8,
            "data": data,
        }
    # Escalonamento do mais específico para o mais genérico. Os três primeiros
    # ramos existem porque o CHATR-125 criou tipos justamente para separar
    # estes desfechos; capturar só `Exception` os achatava de volta num único
    # dict e obrigava quem lê o log a inspecionar a mensagem para saber se a
    # causa foi prazo, infraestrutura ou bug nosso.
    except BigQueryTimeoutError as e:
        # Transitório: a consulta demorou mais que o orçamento, normalmente sob
        # concorrência. `warning`, não `error` — é degradação prevista, e
        # repetir a chamada tem chance real de funcionar.
        logger.warning(f"Timeout na consulta de equipamentos (plus8={plus8}): {e}")
        return {
            "error": "Consulta ao BigQuery excedeu o tempo limite",
            "message": str(e),
        }
    except GoogleAPIError as e:
        # Falha conhecida de infraestrutura (400 de tabela externa, 403 do
        # Drive). Mantém o texto histórico de `error`: é o que já está gravado
        # nos logs do BigQuery e o que o agente já sabe interpretar.
        logger.error(f"Erro no request do bigquery: {e}")
        return {
            "error": "Erro no request do bigquery",
            "message": str(e),
        }
    except BigQueryQueryError as e:
        # A camada de leitura classificou como "não era prazo nem falha
        # conhecida". `exception` para levar o traceback junto: `__cause__`
        # carrega a exceção original, que é a única pista do que de fato houve.
        logger.exception(
            f"Erro INESPERADO na consulta de equipamentos (plus8={plus8}): {e!r}"
        )
        return {
            "error": "Erro inesperado na consulta ao BigQuery",
            "message": str(e),
        }
    except Exception as e:
        # Rede de segurança para o que nem passou pelo BigQuery — montagem da
        # resposta, por exemplo. O contrato com a tool é o mesmo: falha vira
        # dado, nunca exceção que estoura o tool call.
        logger.exception(
            f"Erro INESPERADO ao buscar equipamentos (plus8={plus8}): {e!r}"
        )
        return {
            "error": "Erro inesperado ao buscar equipamentos",
            "message": str(e),
        }


@interceptor(source={"source": "mcp", "tool": "equipments"})
async def get_category_equipments() -> dict:
    query = """
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
            WHERE t.categoria IS NOT NULL and t.equipamentos.use = TRUE
        )

    select *
    from equipamentos eq
    order by eq.secretaria_responsavel, eq.categoria
    """

    # Query sem parâmetros: o namespace sozinho já identifica a consulta.
    data = await get_bigquery_result(
        query=query, cache_namespace="equipments_categories"
    )
    categories = {}
    for d in data:
        if d["secretaria_responsavel"] not in categories:
            categories[d["secretaria_responsavel"]] = []
        categories[d["secretaria_responsavel"]].append(d["categoria"])

    return categories


# Resposta de fallback quando as instruções temáticas não podem ser carregadas.
# É a mesma para falha esperada e inesperada: o contrato com o agente não muda,
# o que muda é só o log. Função (e não constante de módulo) para que ninguém
# mutile acidentalmente o dict devolvido a um chamador anterior.
def _instrucoes_indisponiveis() -> List[dict]:
    return [
        {
            "error": "Instruções temporariamente indisponíveis",
            "message": (
                "Não foi possível carregar as instruções temáticas agora. "
                "Prossiga normalmente: peça o endereço COMPLETO do usuário "
                "(incluindo BAIRRO ou PONTO DE REFERÊNCIA) e chame a tool "
                "`equipments_by_address`. Não mencione esta indisponibilidade "
                "ao cidadão."
            ),
        }
    ]


@interceptor(source={"source": "mcp", "tool": "equipments"})
async def get_tematic_instructions_for_equipments(tema: str = "geral") -> List[dict]:
    # NULL means "no filter" — the WHERE clause handles both cases without
    # any string interpolation of user-supplied values into the SQL text.
    tema_param = tema if tema != "geral" else None

    query = """
        SELECT
            *
        FROM `rj-iplanrio.plus_codes.equipamentos_instrucoes`
        WHERE @tema IS NULL OR tema = @tema
    """
    query_parameters = [
        bigquery.ScalarQueryParameter("tema", "STRING", tema_param),
    ]
    try:
        # `tema` (e não `tema_param`) na chave: os dois são equivalentes um a um,
        # e "geral" é mais legível numa varredura do que a ausência de valor.
        return await get_bigquery_result(
            query=query,
            query_parameters=query_parameters,
            cache_namespace="equipments_instructions",
            cache_key_parts={"tema": tema},
        )
    except GoogleAPIError as e:
        # Caso esperado. `equipamentos_instrucoes` é tabela externa sobre uma
        # Google Sheet (CHATR-119): perder acesso à planilha vira um 400 do
        # BigQuery, que não é `NotFound` e portanto atravessa a degradação de
        # `get_bigquery_result`. Sem este bloco, a falha derruba a tool
        # `equipments_instructions` inteira, e com ela o fluxo de
        # equipamentos — que funciona sem as instruções.
        # O status da tabela é sondado por `src/health/external_tables.py`.
        logger.error(f"Tabela externa de instruções indisponível (tema={tema}): {e}")
        return _instrucoes_indisponiveis()
    except Exception as e:
        # Caso inesperado: bug nosso ou mudança de API, não planilha fora do ar.
        # O cidadão continua protegido pelo mesmo fallback, mas o log precisa
        # ser distinguível — este aqui pede investigação, o de cima não.
        logger.error(
            f"Erro INESPERADO ao buscar instruções de equipamentos (tema={tema}): {e!r}"
        )
        return _instrucoes_indisponiveis()


# if __name__ == "__main__":
# import asyncio

# cat = asyncio.run(get_category_equipments())
# data = asyncio.run(get_pluscode_equipments(address="Avenida Presidente Vargas, 1"))

# print(cat)
# print(data)
