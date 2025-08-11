# -*- coding: utf-8 -*-
"""
Utility functions for the Crawl4AI-based crawler.
This file contains helpers for parsing sitemaps and executing different crawl strategies.
"""

from typing import Any, Dict, List
from urllib.parse import urldefrag, urlparse
from xml.etree import ElementTree
import requests
from src.utils.log import logger
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig


def is_sitemap(url: str) -> bool:
    """Checks if a URL points to a sitemap.xml file."""
    return url.endswith("sitemap.xml") or "sitemap" in urlparse(url).path


def is_txt(url: str) -> bool:
    """Checks if a URL points to a .txt file."""
    return url.endswith(".txt")


def parse_sitemap(sitemap_url: str) -> List[str]:
    """
    Parses a sitemap.xml file and extracts all URLs.

    Args:
        sitemap_url: The URL of the sitemap.

    Returns:
        A list of URLs found in the sitemap.
    """
    try:
        resp = requests.get(sitemap_url, timeout=10)
        resp.raise_for_status()
        tree = ElementTree.fromstring(resp.content)
        urls = [loc.text for loc in tree.findall(".//{*}loc") if loc.text]
        logger.info(f"Parsed {len(urls)} URLs from sitemap: {sitemap_url}")
        return urls
    except requests.RequestException as e:
        logger.error(f"Failed to fetch sitemap {sitemap_url}: {e}")
    except ElementTree.ParseError as e:
        logger.error(f"Failed to parse sitemap XML from {sitemap_url}: {e}")
    return []


async def crawl_batch(
    crawler: AsyncWebCrawler, urls: List[str], config: CrawlerRunConfig
) -> List[Dict[str, Any]]:
    """
    Crawls a list of URLs in parallel, processing results as they arrive.

    Args:
        crawler: An instance of AsyncWebCrawler.
        urls: A list of URLs to crawl.
        config: The CrawlerRunConfig to use for this batch.

    Returns:
        A list of dictionaries, each containing the URL and its markdown content.
    """
    results_list = []
    logger.info(f"Starting batch crawl for {len(urls)} URLs.")

    try:
        # The 'await' gets the async iterator, 'async for' consumes it
        results_iterator = await crawler.arun_many(urls=urls, config=config)
        async for result in results_iterator:
            if result.success and result.markdown:
                logger.info(f"Successfully crawled: {result.url}")
                results_list.append({"url": result.url, "markdown": result.markdown})
            else:
                logger.warning(
                    f"Failed to crawl {result.url}. Status: {result.status_code}, Error: {result.error_message}"
                )
    except Exception as e:
        logger.error(f"An unexpected error occurred during batch crawl: {e}")

    logger.info(f"Batch crawl completed. Successfully extracted {len(results_list)} pages.")
    return results_list


async def crawl_recursive(
    crawler: AsyncWebCrawler,
    start_urls: List[str],
    max_depth: int,
    config: CrawlerRunConfig,
) -> List[Dict[str, Any]]:
    """
    Recursively crawls internal links from a starting set of URLs up to a max depth,
    staying within the same domain.

    Args:
        crawler: An instance of AsyncWebCrawler.
        start_urls: A list of URLs to begin crawling from.
        max_depth: The maximum depth to crawl.
        config: The CrawlerRunConfig to use for the crawl.

    Returns:
        A list of dictionaries, each containing a crawled URL and its markdown content.
    """
    if not start_urls:
        return []

    visited = set()
    results_all = []
    start_domain = urlparse(start_urls[0]).netloc

    def normalize_url(url):
        """Removes URL fragments to avoid duplicate crawling."""
        return urldefrag(url)[0]

    current_urls = {normalize_url(u) for u in start_urls}

    for depth in range(max_depth):
        depth_num = depth + 1
        urls_to_crawl = [url for url in current_urls if url not in visited]

        if not urls_to_crawl:
            logger.info(f"Depth {depth_num}: No new URLs to crawl. Stopping.")
            break

        logger.info(
            f"Starting Depth {depth_num}/{max_depth}: Crawling {len(urls_to_crawl)} URLs."
        )
        visited.update(urls_to_crawl)
        next_level_urls = set()

        try:
            results_iterator = await crawler.arun_many(
                urls=urls_to_crawl, config=config
            )
            async for result in results_iterator:
                if result.success and result.markdown:
                    logger.info(f"  [OK] {result.url}")
                    results_all.append(
                        {"url": result.url, "markdown": result.markdown}
                    )
                    # Collect new internal links for the next level
                    for link in result.links.get("internal", []):
                        next_url = normalize_url(link["href"])
                        # Ensure the link is not visited and is within the same domain
                        if (
                            next_url not in visited
                            and urlparse(next_url).netloc == start_domain
                        ):
                            next_level_urls.add(next_url)
                else:
                    logger.warning(
                        f"  [FAIL] {result.url} | Status: {result.status_code}, Error: {result.error_message}"
                    )
        except Exception as e:
            logger.error(
                f"An unexpected error occurred during recursive crawl at depth {depth_num}: {e}"
            )
            break

        logger.info(
            f"Depth {depth_num} complete. Found {len(next_level_urls)} new URLs for next depth."
        )
        current_urls = next_level_urls

    logger.info(
        f"Recursive crawl finished. Total pages extracted: {len(results_all)}."
    )
    return results_all
