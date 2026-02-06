import os
from typing import Dict, Any, List, Optional, Union
import asyncio
from pathlib import Path
from google import genai
from google.genai.types import (
    Tool,
    UrlContext,
    ThinkingConfig,
    GenerateContentConfig,
    GoogleSearch,
    Content,
    Part,
    GenerateContentResponse,
)
import src.config.env as env
from uuid import uuid4

from datetime import datetime
import httpx
import random

from src.utils.log import logger
from src.utils.error_interceptor import interceptor
from src.utils.http_client import InterceptedHTTPClient
from google.api_core import exceptions as google_exceptions


class GeminiService:
    def __init__(self):
        """Inicializa o cliente Gemini com as configurações do ambiente."""
        self.api_key = env.GEMINI_API_KEY
        self.client = genai.Client(api_key=self.api_key)

    def get_client(self):
        """Retorna a instância do cliente Gemini."""
        return self.client

    @interceptor(source={"source": "mcp", "tool": "gemini"})
    async def google_search(
        self,
        query: str,
        model: str = "gemini-2.5-flash",
        temperature: float = 0.0,
        retry_attempts: int = 1,
    ):
        logger.info(f"Iniciando pesquisa Google para: {query}")
        request_id = str(uuid4())
        last_exception = None
        formatted_prompt = web_searcher_instructions(research_topic=query)
        for attempt in range(retry_attempts):
            try:
                # Timeout total para toda a operação
                async with asyncio.timeout(180):  # 180 segundos para toda a operação
                    logger.info(f"Prompt Length: {len(formatted_prompt)}")
                    tools = [
                        Tool(google_search=GoogleSearch()),
                        Tool(url_context=UrlContext()),
                    ]

                    response = await self.client.aio.models.generate_content(
                        model=model,
                        contents=[
                            Content(role="user", parts=[Part(text=formatted_prompt)])
                        ],
                        config=GenerateContentConfig(
                            temperature=temperature,
                            thinking_config=ThinkingConfig(
                                thinking_budget=-1,
                            ),
                            tools=tools,
                            response_mime_type="text/plain",
                        ),
                    )

                    logger.info("Resposta recebida do Gemini")
                    
                    if not response.candidates or len(response.candidates) == 0:
                        logger.warning("Resposta sem candidatos válidos do Gemini")
                        if attempt >= retry_attempts - 1:
                            return {
                                "id": request_id,
                                "text": "Não foi possível obter uma resposta válida para esta consulta. Por favor, tente reformular sua pergunta ou tente novamente mais tarde.",
                                "sources": [],
                                "web_search_queries": [],
                                "tokens_metadata": {},
                                "retry_attempts": attempt + 1,
                                "model": model,
                                "temperature": temperature,
                                "query": query,
                            }
                        continue
                    
                    candidate = response.candidates[0]

                    # Check if grounding metadata and chunks exist
                    if (
                        not candidate.grounding_metadata
                        or not candidate.grounding_metadata.grounding_chunks
                    ):
                        # No grounding chunks found - likely all sources were filtered or unavailable
                        logger.warning(
                            "Nenhuma fonte confiável encontrada para a consulta"
                        )
                        return {
                            "id": request_id,
                            "text": "Desculpe, mas não consegui encontrar informações confiáveis sobre este tópico em fontes oficiais. Esta consulta pode estar fora do escopo do meu conhecimento, que se concentra em serviços municipais do Rio de Janeiro e fontes governamentais oficiais.",
                            "sources": [],
                            "web_search_queries": [],
                            "tokens_metadata": {},
                            "retry_attempts": attempt,
                            "model": model,
                            "temperature": temperature,
                            "query": query,
                        }

                    logger.info("Resolvendo URLs das fontes...")
                    resolved_urls_map = await resolve_urls(
                        urls_to_resolve=candidate.grounding_metadata.grounding_chunks
                    )

                    citations = get_citations(
                        response=response, resolved_urls_map=resolved_urls_map
                    )
                    modified_text = format_text_with_citations(response.text, citations)
                    sources_gathered = get_sources_list(citations, modified_text)

                    # Check if all sources were filtered out by blacklist
                    if not sources_gathered and citations:
                        logger.warning("Todas as fontes encontradas estão na blacklist")
                        return {
                            "id": request_id,
                            "text": "Desculpe, mas as informações encontradas para esta consulta vêm de fontes que estão fora do escopo do meu conhecimento. Eu me concentro em fornecer informações sobre serviços municipais do Rio de Janeiro usando apenas fontes governamentais oficiais e confiáveis.",
                            "sources": [],
                            "web_search_queries": (
                                candidate.grounding_metadata.web_search_queries
                                if candidate.grounding_metadata
                                else []
                            ),
                            "tokens_metadata": self.get_tokens_metadata(
                                response=response
                            ),
                            "retry_attempts": attempt,
                            "model": model,
                            "temperature": temperature,
                            "query": query,
                        }

                    web_search_queries = []
                    if (
                        candidate.grounding_metadata
                        and candidate.grounding_metadata.web_search_queries
                    ):
                        web_search_queries = (
                            candidate.grounding_metadata.web_search_queries
                        )
                    tokens_metadata = self.get_tokens_metadata(response=response)

                    logger.info(
                        f"Pesquisa concluída com {len(sources_gathered)} fontes"
                    )

                    return {
                        "id": request_id,
                        "text": modified_text,
                        "sources": sources_gathered,
                        "web_search_queries": web_search_queries,
                        "tokens_metadata": tokens_metadata,
                        "retry_attempts": attempt,
                        "model": model,
                        "temperature": temperature,
                        "query": query,
                    }

            except (
                google_exceptions.PermissionDenied,
                google_exceptions.InvalidArgument,
            ) as e:
                logger.error(
                    f"Erro não recuperável na API Google: {e}. Não haverá nova tentativa."
                )
                last_exception = e
                break  # Interrompe em erros de cliente (4xx)

            except (
                asyncio.TimeoutError,
                google_exceptions.InternalServerError,
                google_exceptions.ServiceUnavailable,
                google_exceptions.ResourceExhausted,
            ) as e:
                last_exception = e
                logger.warning(
                    f"Erro transiente na API Google: {e}. Tentando novamente..."
                )

            except Exception as e:
                last_exception = e
                logger.error(f"Erro inesperado durante a pesquisa Google: {e}")

            # Se não for a última tentativa, espera antes de tentar novamente
            if attempt < retry_attempts - 1:
                # Exponential backoff com jitter
                wait_time = (2**attempt) + random.uniform(0, 1)
                logger.info(
                    f"Tentativa {attempt + 1}/{retry_attempts} falhou. "
                    f"Aguardando {wait_time:.2f}s para a próxima tentativa."
                )
                await asyncio.sleep(wait_time)

        # Se todas as tentativas falharem
        logger.error(
            f"Todas as {retry_attempts} tentativas de pesquisa falharam para a query: {query}. "
            f"Último erro: {last_exception}"
        )
        return {
            "id": request_id,
            "text": f"Erro na pesquisa Google após {retry_attempts} tentativas: {last_exception}",
            "sources": [],
            "web_search_queries": [],
            "tokens_metadata": {},
            "retry_attempts": retry_attempts,
            "model": model,
            "temperature": temperature,
            "query": query,
        }

    def get_tokens_metadata(self, response: GenerateContentResponse) -> dict:
        usage_metadata = response.usage_metadata
        tokens_metadata = {}
        if usage_metadata:
            # Helper function to convert ModalityTokenCount to dict
            def convert_modality_token_count(item):
                if item is None:
                    return None
                if isinstance(item, list):
                    return [
                        (
                            {
                                "modality": tc.modality.value,
                                "token_count": tc.token_count,
                            }
                            if hasattr(tc, "modality")
                            else tc
                        )
                        for tc in item
                    ]
                if hasattr(item, "modality"):
                    return {
                        "modality": item.modality.value,
                        "token_count": item.token_count,
                    }
                return item

            tokens_metadata["cache_tokens_details"] = convert_modality_token_count(
                usage_metadata.cache_tokens_details
            )
            tokens_metadata["cached_content_token_count"] = (
                usage_metadata.cached_content_token_count
            )
            tokens_metadata["candidates_token_count"] = (
                usage_metadata.candidates_token_count
            )
            tokens_metadata["candidates_tokens_details"] = convert_modality_token_count(
                usage_metadata.candidates_tokens_details
            )
            tokens_metadata["prompt_token_count"] = usage_metadata.prompt_token_count
            tokens_metadata["thoughts_token_count"] = (
                usage_metadata.thoughts_token_count
            )
            tokens_metadata["tool_use_prompt_token_count"] = (
                usage_metadata.tool_use_prompt_token_count
            )
            tokens_metadata["tool_use_prompt_tokens_details"] = (
                convert_modality_token_count(
                    usage_metadata.tool_use_prompt_tokens_details
                )
            )
            tokens_metadata["total_token_count"] = usage_metadata.total_token_count

        return tokens_metadata


gemini_service = GeminiService()


async def process_link(session, link: dict):
    uri = link.get("uri")

    # Timeout específico por requisição (menor que o timeout geral)
    link_timeout = 5

    try:
        # Primeiro tenta HEAD request (mais rápido)
        response = await session.head(uri, follow_redirects=True, timeout=link_timeout)
        response.raise_for_status()
        link["url"] = str(response.url)
        link["error"] = None
        return link
    except Exception as e:
        try:
            # Se HEAD falhar, tenta GET request
            response = await session.get(
                uri, follow_redirects=True, timeout=link_timeout
            )
            response.raise_for_status()
            link["url"] = str(response.url)
            link["error"] = None
            return link

        except Exception as e2:
            error_msg = str(e2)
            # Trata erro específico do Mozilla
            mozilla_suffix = (
                "For more information check: https://developer.mozilla.org/"
            )
            if mozilla_suffix in error_msg:
                try:
                    msg = error_msg.replace(mozilla_suffix, "")
                    msg = msg.split("http")[1]
                    msg = msg.split("'\n")[0]
                    msg = "http" + msg
                    link["url"] = msg
                    link["error"] = None
                except:
                    # Se parsing falhar, usa URI original
                    link["url"] = uri
                    link["error"] = f"Timeout ou erro de conexão: {error_msg}"
            else:
                link["url"] = uri
                link["error"] = f"Erro ao resolver URL: {error_msg}"
            return link


@interceptor(source={"source": "mcp", "tool": "gemini", "function": "resolve_urls"})
async def resolve_urls(urls_to_resolve: List[Any]) -> Dict[str, str]:
    """
    Create a map of the vertex ai search urls (very long) to a short url with a unique id for each url.
    Ensures each original URL gets a consistent shortened form while maintaining uniqueness.
    """
    unique_urls = list(set([uri.web.uri for uri in urls_to_resolve]))
    urls = [{"uri": uri} for uri in unique_urls]

    # logger.info(f"Resolvendo {len(urls)} URLs únicas")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"
    }

    async with InterceptedHTTPClient(
        user_id="unknown",
        source={"source": "mcp", "tool": "gemini", "function": "resolve_urls"},
        timeout=30.0,
        follow_redirects=True,
        verify=False,
        headers=headers
    ) as session:
        # Limita concorrência para evitar sobrecarga
        semaphore = asyncio.Semaphore(20)

        async def process_with_semaphore(link):
            async with semaphore:
                return await process_link(session, link)

        results = await asyncio.gather(
            *(process_with_semaphore(link) for link in urls), return_exceptions=True
        )

        # Trata exceções não capturadas
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                # logger.error(
                #     f"Erro não tratado ao processar URL {urls[i]['uri']}: {result}"
                # )
                urls[i]["url"] = urls[i]["uri"]
                urls[i]["error"] = str(result)
            else:
                urls[i] = result

    resolved_map = {
        link["uri"]: {"url": link["url"], "error": link["error"]} for link in urls
    }

    successful_resolutions = sum(1 for link in urls if link["error"] is None)
    logger.info(f"URLs resolvidas com sucesso: {successful_resolutions}/{len(urls)}")

    return resolved_map


def get_citations(response, resolved_urls_map):
    """
    Extracts and formats citation information from a Gemini model's response.

    This function processes the grounding metadata provided in the response to
    construct a list of citation objects. Each citation object includes the
    start and end indices of the text segment it refers to, and a string
    containing formatted markdown links to the supporting web chunks.
    """
    citations = []

    # Ensure response and necessary nested structures are present
    if not response or not response.candidates:
        return citations

    candidate = response.candidates[0]
    if (
        not hasattr(candidate, "grounding_metadata")
        or not candidate.grounding_metadata
        or not hasattr(candidate.grounding_metadata, "grounding_supports")
    ):
        return citations

    for support in candidate.grounding_metadata.grounding_supports:
        citation = {}

        # Ensure segment information is present
        if not hasattr(support, "segment") or support.segment is None:
            continue  # Skip this support if segment info is missing

        start_index = (
            support.segment.start_index
            if support.segment.start_index is not None
            else 0
        )

        # Ensure end_index is present to form a valid segment
        if support.segment.end_index is None:
            continue  # Skip if end_index is missing, as it's crucial

        # Add 1 to end_index to make it an exclusive end for slicing/range purposes
        # (assuming the API provides an inclusive end_index)
        citation["start_index"] = start_index
        citation["end_index"] = support.segment.end_index

        citation["segments"] = []
        if (
            hasattr(support, "grounding_chunk_indices")
            and support.grounding_chunk_indices
        ):
            for ind in support.grounding_chunk_indices:
                try:
                    chunk = candidate.grounding_metadata.grounding_chunks[ind]
                    resolved_url = resolved_urls_map.get(chunk.web.uri, None)

                    # Skip if resolved_url is None or doesn't have the expected structure
                    if not resolved_url or "url" not in resolved_url:
                        continue

                    # Check if the URL's domain is blacklisted
                    from urllib.parse import urlparse

                    try:
                        parsed_url = urlparse(resolved_url["url"])
                        domain = parsed_url.netloc.lower()

                        # Get blacklisted domains from environment
                        blacklisted_domains = env.LINK_BLACKLIST
                        is_blacklisted = any(
                            blacklisted_domain.strip().lower() in domain
                            for blacklisted_domain in blacklisted_domains
                            if blacklisted_domain.strip()
                        )

                        # Only add to citations if not blacklisted
                        if not is_blacklisted:
                            citation["segments"].append(
                                {
                                    "label": chunk.web.title,
                                    "uri": chunk.web.uri,
                                    "url": resolved_url["url"],
                                    # "text": support.segment.text,
                                    # "error": resolved_url["error"],
                                }
                            )
                    except Exception:
                        # If there's any error with URL parsing or blacklist checking,
                        # fall back to the original behavior (include the citation)
                        citation["segments"].append(
                            {
                                "label": chunk.web.title,
                                "uri": chunk.web.uri,
                                "url": resolved_url["url"],
                                # "text": support.segment.text,
                                # "error": resolved_url["error"],
                            }
                        )
                except (IndexError, AttributeError, NameError):
                    # Handle cases where chunk, web, uri, or resolved_map might be problematic
                    # For simplicity, we'll just skip adding this particular segment link
                    # In a production system, you might want to log this.
                    pass
        citations.append(citation)
    return citations


def format_text_with_citations(text, citations_data):
    """
    Insere marcadores de citação em um texto de forma inteligente,
    posicionando-os no final das palavras ou frases.

    Args:
        text (str): O texto original.
        citations_data (list): Uma lista de dicionários com os dados da citação.

    Returns:
        str: O texto modificado com as citações e a lista de fontes.
    """
    modified_text = text

    # 1. Mapear URLs para números de citação únicos para evitar duplicatas.
    # Usamos o 'index' fornecido nos dados ou criamos um novo se não houver.
    source_map = {}
    next_citation_num = 1
    # Pré-processa para atribuir números de citação únicos e estáveis
    temp_citations = []
    for citation in citations_data:
        # A maioria dos segmentos tem um 'index' que podemos usar
        try:
            # Skip citations with no segments (filtered out by blacklist)
            if not citation["segments"]:
                continue

            url = citation["segments"][0]["url"]
            if url not in source_map:
                source_map[url] = citation["segments"][0].get(
                    "index", next_citation_num
                )
                if "index" not in citation["segments"][0]:
                    next_citation_num += 1

            # Adiciona a citação com o número correto para processamento
            citation_num = source_map[url]
            temp_citations.append({**citation, "citation_num": citation_num})
        except (KeyError, IndexError):
            # Ignora citações malformadas ou vazias
            continue

    # 2. Ordenar as citações pelo 'end_index' em ordem DECRESCENTE.
    # Isso é CRUCIAL para que as inserções não afetem os índices das próximas.
    sorted_citations = sorted(
        temp_citations, key=lambda c: c["end_index"], reverse=True
    )

    # 3. Iterar e inserir os marcadores de citação
    for citation in sorted_citations:
        end_index = citation["end_index"]
        citation_num = citation["citation_num"]

        # O marcador a ser inserido (com um espaço antes para formatação)
        marker = f" [{citation_num}]"

        # 4. Encontrar a posição de inserção ideal
        # Começa no end_index e avança até o final da palavra (encontra um espaço, pontuação, etc.)
        insertion_pos = end_index
        while (
            insertion_pos < len(modified_text)
            and modified_text[insertion_pos].isalnum()
        ):
            insertion_pos += 1

        # Insere o marcador na posição encontrada
        modified_text = (
            modified_text[:insertion_pos] + marker + modified_text[insertion_pos:]
        )

    # 5. Gerar a lista de fontes formatada no final
    sources_list = "\n\n**Sources:**\n"
    # Inverte o mapa para ordenar por número de citação
    sorted_sources = sorted(source_map.items(), key=lambda item: item[1])

    for url, num in sorted_sources:
        # Pega o 'label' da primeira vez que a fonte apareceu
        label = next(
            (
                c["segments"][0]["label"]
                for c in citations_data
                if c["segments"] and c["segments"][0]["url"] == url
            ),
            url,
        )
        sources_list += f" - [{num}] {url}\n"

    return modified_text + sources_list


def get_sources_list(citations_data, modified_text):
    all_segments = (
        segment for citation in citations_data for segment in citation["segments"]
    )
    seen_uris = set()
    sources_gathered = []

    # Get blacklisted domains from environment
    blacklisted_domains = env.LINK_BLACKLIST

    for source in all_segments:
        if source["uri"] not in seen_uris and source["url"] in modified_text:
            try:
                # Check if the URL's domain is in the blacklist
                from urllib.parse import urlparse

                parsed_url = urlparse(source["url"])
                domain = parsed_url.netloc.lower()

                # Check if any blacklisted domain matches the source domain
                is_blacklisted = any(
                    blacklisted_domain.strip().lower() in domain
                    for blacklisted_domain in blacklisted_domains
                    if blacklisted_domain.strip()
                )

                if not is_blacklisted:
                    seen_uris.add(source["uri"])
                    sources_gathered.append(source)
            except Exception:
                # If there's any error with URL parsing or blacklist checking,
                # fall back to the original behavior (include the source)
                seen_uris.add(source["uri"])
                sources_gathered.append(source)

    for index, source in enumerate(sources_gathered):
        source["index"] = index + 1

    return sources_gathered


def web_searcher_instructions(research_topic: str):
    current_date = datetime.now().strftime("%Y-%m-%d")

    return f"""### Persona
You are a diligent and meticulous Investigative Research Analyst specializing in Rio de Janeiro municipal government services. Your superpower is sifting through web search results to find the ground truth. You are skeptical, fact-driven, and obsessed with source integrity. You always use the `google_search` tool to find the ground truth using at least 5 different queries.

### Objective
Your mission is to execute a web search query, critically evaluate the top results, and synthesize the findings into a clear, factual, and perfectly cited text artifact. The quality of your work is the foundation for all subsequent analysis.

### Execution Workflow
You must follow this precise four-step process for every query:

1.  **Analyze Query Intent:** Before anything else, understand the specific goal of the provided query. This context will guide your evaluation.

2.  **Evaluate Search Results:** As you process the search results, you must act as a critical filter. Prioritize and weigh information based on these criteria:
    *   **Authority & Trustworthiness:** Strongly prefer official Rio de Janeiro municipal sources, like official domains (**carioca.rio, prefeitura.rio, 1746.rio, cor.rio, .gov.br**), official city departments, and municipal secretariats. \
        For electronic ticketing for public transport, or "RioCard" consider https://jae.com.br/central-de-ajuda/ as a source. \
        Whenever the user's question is about getting, renewing, requesting a duplicate, or finding service locations for the "Jae" card/"Riocard", the answer **must always** include the official link, which is: https://jae.com.br/onde-estamos/. \
        For workshops, courses and events related to Basic Computing, Creative Economy, Information Technologies, Robotics and Programming, Work and Entrepreneurship and Artificial Intelligence consider https://www.navedoconhecimento.rio/ as a source. \
        Do NOT quote the program RJPet/website rjpet.com.br as it does not exist anymore. \
        Informations about subprefecture should be searched in this official website: https://gbp.prefeitura.rio/subprefeituras/. \
        When providing guidance on how to pay traffic fines, you should always point out that there are three different types of violations: municipal, state, and federal. \
        To help users access their fines, always include the following links: Municipal Fines: https://carioca.rio/servicos/multa-de-transito-informacoes-sobre-como-pagar/ ; State Fines: https://www.detran.rj.gov.br/infracoes/pagamento-de-multas/como-pagar.html ; Federal Fines: https://servicos.dnit.gov.br/multas/ ; \
        Also, always mention that there is a mobile app called 'CDT - Carteira Digital de Trânsito' that allows you to check all three types of violations. \
        Secondary preference for established news sources covering Rio municipal affairs. \
        Avoid unofficial blogs, forums, opinion pieces, or generalist portals. \
        When login via Gov.br is available, inform the user about it, as it is the official government authentication system. \

    *   **Recency:** Check the publication date. For the given query, is older information still relevant, or is it critical to find the most recent data? The current date is **{current_date}**.
    *   **Objectivity:** Differentiate between factual reporting and biased commentary. Your synthesis must be based on facts.

3.  **Synthesize & Extract:** Consolidate the verified facts from the most reliable sources into an extensive and detailed summary.

4.  **Integrate Citations Flawlessly:** As you write, you MUST cite your sources.
    *   Use citations in an academic style, such as [source_name], immediately after the relevant fact or statement.
    *   If an entire paragraph is synthesized from a single source, you may place a single citation at the end of that paragraph.
    *   Your final answer is only credible if it is fully verifiable. Every key fact must be traceable to its source.

### Critical Directives
- **ZERO HALLUCINATION:** NEVER invent information, statistics, or sources. If the search results for the query are empty, inconclusive, or of poor quality, you MUST state: "No reliable information could be found for this query."
- **STRICT NEUTRALITY:** Your job is to report the facts, not interpret them. Do not add your own analysis, conclusions, or opinions. Present the synthesized information in a neutral, encyclopedic tone.
- **FOCUS ON THE QUERY:** Your synthesis must ONLY answer the specific research topic provided. Do not include interesting but tangential information from the sources. Stick to the mission.

### Output Format
A long, detailed markdown "Research Artifact" with all the information from the sources and the citations and the date of the search.


### Your Turn

**Research Topic:**
{research_topic}
"""
