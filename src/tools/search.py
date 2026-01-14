import asyncio
import json

from src.tools.google_search.gemini_service import gemini_service
from src.utils.bigquery import save_response_in_bq_background
from src.utils.typesense_api import HubSearchRequest, hub_search
from src.utils.error_interceptor import interceptor

from src.config import env

from src.utils.log import logger


@interceptor(source={"source": "mcp", "tool": "search"})
async def get_google_search(query: str):
    """
    Obtém resultados de busca via Typesense (se ativo) ou Google Search como fallback.
    """

    # 1. Configurações iniciais e Logs
    is_typesense_active = (
        env.TYPESENSE_ACTIVE == "true" and env.TYPESENSE_HUB_SEARCH_URL
    )
    logger.info(f"Typesense Active: {is_typesense_active}")

    response_data = None
    source = "google"
    final_response = {}
    # 2. Tentativa com Typesense (se habilitado)
    if is_typesense_active:
        logger.info("Using Typesense Hub Search")

        # Parse de parâmetros de forma segura (evitando eval)
        params = _get_typesense_params()
        logger.info(
            f"Typesense Parameters: {json.dumps(params, indent=2, ensure_ascii=False)}"
        )

        hub_request = HubSearchRequest(
            q=query,
            type=params.get("type", "semantic"),
            threshold_semantic=params.get("threshold_semantic", 0.7),
            threshold_hybrid=params.get("threshold_hybrid", 0.7),
            alpha=params.get("alpha", 0.7),
            page=params.get("page", 1),
            per_page=params.get("per_page", 10),
            # generate_scores=(params.get("type") == "ai"),
            # threshold_keyword=params.get("threshold_keyword", 1),
            # threshold_ai=params.get("threshold_ai", 0.85),
        )

        typesense_res = await hub_search(request=hub_request)

        # Se encontrou resultados no Typesense
        if (
            typesense_res
            and typesense_res.get("results")
            and len(typesense_res.get("results", [])) > 0
        ):
            source = "typesense"
            response_data = typesense_res
            final_response = {"response": typesense_res.get("results_clean")}
            response_data.pop("results_clean")
            # logger.debug(
            #     f"Typesense Response: {json.dumps(response_data, indent=2, ensure_ascii=False)} "
            # )
            # logger.debug(
            #     f"Typesense Response: {json.dumps(typesense_res, indent=2, ensure_ascii=False)} "
            # )
    # 3. Fallback ou Execução Principal: Google Search
    # Executa se o Typesense estiver desativado OU se o Typesense não retornou resultados
    if not response_data:
        logger.info("Executing Google Search via Gemini Service")
        source = "google"
        response_google = await gemini_service.google_search(
            query=query,
            model=env.GEMINI_MODEL,
            temperature=0.0,
            retry_attempts=1,
        )
        response_data = response_google
        final_response = {
            "text": response_google.get("text"),
            "sources": response_google.get("sources"),
            "web_search_queries": response_google.get("web_search_queries"),
            "id": response_google.get("id"),
        }

    # 4. Log em Background (BigQuery)
    asyncio.create_task(
        save_response_in_bq_background(
            data={"source": source, "response": response_data},
            endpoint="/tools/google_search",
            dataset_id="brutos_eai_logs",
            table_id="mcp",
        )
    )

    return final_response


def _get_typesense_params():
    """Auxiliar para processar os parâmetros do Typesense com segurança."""
    defaults = {
        "type": "semantic",
        "threshold_semantic": 0.7,
        "threshold_hybrid": 0,
        "alpha": 0,
        "page": 1,
        "per_page": 5,
        "threshold_ai": 0,
        "threshold_keyword": 0,
    }

    raw_params = env.TYPESENSE_PARAMETERS

    if raw_params == "none":
        return defaults

    if isinstance(raw_params, str):
        try:
            return json.loads(raw_params)
        except json.JSONDecodeError as e:
            logger.info(f"raw_params: {raw_params}")
            logger.error("Failed to parse TYPESENSE_PARAMETERS as JSON")
            logger.error(f"{e}")
            return defaults

    return raw_params if isinstance(raw_params, dict) else defaults
