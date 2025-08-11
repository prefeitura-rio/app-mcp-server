# -*- coding: utf-8 -*-
import asyncio
import json
import re
from urllib.parse import urlparse

from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
from src.utils.log import logger
from src.tools.crawl_pages.utils import (
    crawl_batch,
    crawl_recursive,
    is_sitemap,
    is_txt,
    parse_sitemap,
)


def _format_code_blocks(md_content: str) -> str:
    """
    Identifies and formats python code blocks in markdown that are missing language identifiers.
    """

    def replacer(match):
        block_content = match.group(1)
        lines = block_content.split("\n", 1)
        first_line = lines[0].strip() if lines else ""

        # A list of common language identifiers to check against
        known_languages = [
            "python",
            "py",
            "json",
            "bash",
            "sh",
            "javascript",
            "js",
            "html",
            "css",
            "sql",
            "yaml",
            "yml",
            "typescript",
            "ts",
        ]
        if first_line in known_languages:
            # Already formatted, return as is
            return match.group(0)

        # Heuristics to detect Python code
        # Using regex for more specific matching
        python_indicators = [
            r"\bimport\s+",
            r"\bfrom\s+.*\s+import\s+",
            r"\bdef\s+.*\):",
            r"\bclass\s+.*\:",
            r"\basync\s+def\s+",
            r"^\s*@",  # Decorators
        ]
        # Check the whole block for these indicators
        if any(
            re.search(pattern, block_content, re.MULTILINE)
            for pattern in python_indicators
        ):
            return f"```python\n{block_content.strip()}\n```"

        # If no language detected, return the original block
        return match.group(0)

    # This regex finds all code blocks (```...```)
    return re.sub(r"```(.*?)```", replacer, md_content, flags=re.DOTALL)


async def smart_crawl_url(
    url: str, max_depth: int = 3, max_concurrent: int = 10, chunk_size: int = 5000
):
    """
    Intelligently crawls a URL by determining its type and applying the best strategy.

    Args:
        url: The URL to crawl (can be a webpage, sitemap.xml, or .txt file).
        max_depth: Maximum recursion depth for webpages.
        max_concurrent: Maximum number of concurrent browser sessions.
        chunk_size: This parameter is noted but not used in the current implementation.
    """
    # Centralized configuration for browser and crawler
    browser_config = BrowserConfig(headless=True, verbose=False)
    base_run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        semaphore_count=max_concurrent,
        page_timeout=60000,  # 60-second timeout for each page
        magic=True,  # Enable auto-handling of popups and common patterns
        scan_full_page=True,  # Ensure dynamic content is loaded
        verbose=True,  # Get detailed logs from the crawler
    )

    crawl_results = []
    crawl_type = "unknown"

    # Use 'async with' for proper resource management of the crawler
    async with AsyncWebCrawler(config=browser_config) as crawler:
        if is_txt(url):
            crawl_type = "text_file"
            logger.info(f"URL is a {crawl_type}, performing a single page crawl.")
            # For a single file, we can use a simpler config
            single_file_config = base_run_config.clone(stream=False)
            result = await crawler.arun(url=url, config=single_file_config)
            if result.success and result.markdown:
                crawl_results.append({"url": url, "markdown": result.markdown})
            elif not result.success:
                logger.error(f"Failed to crawl {url}: {result.error_message}")

        elif is_sitemap(url):
            crawl_type = "sitemap"
            logger.info(f"URL is a {crawl_type}, parsing and crawling in batch.")
            sitemap_urls = parse_sitemap(url)
            if not sitemap_urls:
                return json.dumps(
                    {"success": False, "url": url, "error": "No URLs found in sitemap"},
                    indent=2,
                )
            # Use streaming for batch processing to get real-time feedback
            batch_config = base_run_config.clone(stream=True)
            crawl_results = await crawl_batch(crawler, sitemap_urls, batch_config)

        else:
            crawl_type = "webpage"
            logger.info(f"URL is a {crawl_type}, performing recursive crawl.")
            # Use streaming for recursive crawling
            recursive_config = base_run_config.clone(stream=True)
            crawl_results = await crawl_recursive(
                crawler, [url], max_depth, recursive_config
            )

    if not crawl_results:
        return f"# Crawl Failed\n\n**URL:** {url}\n\n**Error:** No content could be extracted."

    # Process results into a single Markdown string
    markdown_outputs = []
    for doc in crawl_results:
        source_url = doc["url"]
        md_content = doc["markdown"]
        parsed_url = urlparse(source_url)
        source_id = parsed_url.netloc or parsed_url.path

        # Clean the markdown content
        # 1. Remove markdown images: ![alt text](url)
        md_content = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md_content)
        # 2. Remove code line number links: [](url)
        md_content = re.sub(r"\[\]\([^\]]*\)", "", md_content)
        # 3. Format code blocks
        md_content = _format_code_blocks(md_content)

        meta = {
            "url": source_url,
            "source": source_id,
            "crawl_type": crawl_type,
            "word_count": len(md_content.split()),
        }

        # Format metadata as a pretty-printed JSON string for the markdown code block
        metadata_json = json.dumps(meta, indent=2)

        # Construct the markdown chunk for this document
        doc_markdown = f"""
## Source: {source_url}

### Metadata

```json
{metadata_json}
```

### Content

{md_content}
"""
        markdown_outputs.append(doc_markdown.strip())

    # Join all markdown chunks with a separator
    return "\n\n---\n\n".join(markdown_outputs)