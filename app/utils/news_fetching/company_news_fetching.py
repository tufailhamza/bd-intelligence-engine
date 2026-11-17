import asyncio
import httpx
import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Dict, Optional
from urllib.parse import parse_qs, unquote, urlparse

import numpy as np
import openai
from dateutil import parser as date_parser
from dotenv import load_dotenv
from openai import AsyncClient
from sklearn.metrics.pairwise import cosine_similarity

from app.database.utilities import experimental_cache_data
from app.utils.news_fetching.news_service import company_news
from app.utils.news_fetching.news_utils import filter_news_based_on_date, is_english
from app.utils.news_fetching.news_relevance import call_llm_for_news_relevance
from app.utils.news_fetching.prompts import RELEVANCE_PROMPT
from crawl4ai import AsyncWebCrawler
from crawl4ai.async_configs import BrowserConfig, CrawlerRunConfig
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
from qutils.llm.utilities import asynchronous_llm

logger = logging.getLogger(__name__)


load_dotenv()

openai.api_key = os.getenv("OPENAI_API_KEY")


def extract_published_date(snippet: str) -> str | None:
    try:
        # Match "May 6, 2024" style
        match = re.match(r"^([A-Za-z]+\s+\d{1,2},\s+\d{4})", snippet)
        if match:
            dt = date_parser.parse(match.group(1))
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Match "6 days ago" style
        match = re.match(r"^(\d+)\s+days?\s+ago", snippet)
        if match:
            days_ago = int(match.group(1))
            dt = datetime.utcnow() - timedelta(days=days_ago)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        # No date info at the start
        return None
    except Exception:
        return None


async def generate_yahoo_query(company_universal_name: str) -> str:
    keywords = [
        "funding",
        "IPO",
        "hiring",
        "executive",
        "recruitment",
        "acquisition",
        "merger",
        "buyout",
        "expansion",
        "product launch",
        "new product",
        "partnership",
        "CEO",
        "CFO",
        "board appointment",
        "earnings",
        "quarterly results",
        "financial results",
    ]

    # Quote multi-word keywords, leave single words as-is
    quoted_keywords = [f'"{kw}"' if " " in kw else kw for kw in keywords]

    # Construct the OR part
    or_part = " OR ".join(quoted_keywords)

    # Format the full query
    query = f"+{company_universal_name} ({or_part})"
    return query


async def proxy_inference(prompt: str, timeout: int = 15) -> dict | None:
    """
    Perform proxy inference for the given query.
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.get(
                os.getenv("CLOUDFUNCTION_SERVICE"),
                headers={"accept": "application/json"},
                params={"query": prompt, "search_engine": "yahoo"},
            )
            response.raise_for_status()
            return response.json()
        except httpx.RequestError as e:
            logger.error(f"Request error: {e}")
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP status error: {e}")
        except httpx.TimeoutException as e:
            logger.error(f"Request timed out: {e}")
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
        return None


async def web_search(query: str, MAX_RETRIES=5) -> list:
    """
    Perform a web search for the given query and return extracted snippets.
    """

    response = None

    for attempt in range(MAX_RETRIES):
        try:
            response = await proxy_inference(query)
            if response and "error" not in response:
                break
            else:
                logger.error("error in proxy inference")
        except Exception as e:
            logger.error(
                f"Attempt {attempt + 1} proxy inference failed with error: {e}"
            )
            if attempt == MAX_RETRIES - 1:
                raise

    if not response:
        logger.info(f"Failed to fetch results for query: {query}")
        return []

    structured_results = [
        {
            "title": result.get("title", ""),
            "snippet": result.get("snippet", ""),
            "link": result.get("link", ""),
            "query": query,
        }
        for result in response.get("query_result", [])
        if "snippet" in result and "title" in result and "link" in result
    ]

    return structured_results


def extract_and_decode_url(encoded_url: str) -> str | None:
    # Handle Yahoo search redirect
    if "r.search.yahoo" in encoded_url:
        parts = encoded_url.split("/")
        for part in parts:
            if part.startswith("RU="):
                encoded_part = part[3:]
                return unquote(encoded_part)

    # Handle Google redirect
    if "www.google.com/url" in encoded_url:
        parsed = urlparse(encoded_url)
        query_params = parse_qs(parsed.query)
        url_param = query_params.get("url", [None])[0]
        if url_param:
            return unquote(url_param)

    # No redirect detected, return as-is
    return encoded_url


async def fetch_clean_text(
    link: str, crawler: object, crawl_config: object
) -> str | None:

    browser_config = BrowserConfig(verbose=False, text_mode=True)

    md_generator = DefaultMarkdownGenerator(
        options={
            "ignore_links": True,
            "escape_html": False,
            "skip_internal_links": True,
        }
    )

    crawl_config = CrawlerRunConfig(
        markdown_generator=md_generator,
        excluded_tags=["form", "header", "input", "button", "nav", "footer"],
        exclude_external_links=True,
        remove_overlay_elements=True,
        cache_mode=False,
        page_timeout=15000,
    )

    crawler = AsyncWebCrawler(config=browser_config)
    await crawler.start()

    try:
        cleaned_url = link
        if not cleaned_url:
            return None

        result = await crawler.arun(
            url=cleaned_url, config=crawl_config, session_id="1"
        )

        await crawler.close()

        if result.success:
            return result.markdown
        else:
            return None
    except Exception as e:
        await crawler.close()
        logger.error(f"Error Crawl4AI = {e}")
        return None


async def web_search_and_crawl(query: str, company: str) -> list:
    if isinstance(query, dict):
        query = query["query"]

    results = await web_search(query)

    if results:
        if isinstance(results, dict) and results.get("error") is True:
            return None
    else:
        return None

    filtered_results = []

    if results:
        for item in results:
            item.pop("query", None)
            item["link"] = extract_and_decode_url(item["link"])
            item["published_date"] = extract_published_date(item.get("snippet", ""))
            if item["published_date"] is not None and is_english(item.get("title", "")):
                filtered_results.append(item)
    else:
        return None

    if len(filtered_results) > 0:
        results = filtered_results
    else:
        return None

    # Step 1: Create structured input with title and link inside tags, plus reverse lookup
    structured_input = ""
    reverse_lookup = {}
    for idx, item in enumerate(results, start=1):
        title = item.get("title", "").strip()
        snippet = item.get("snippet", "").strip()
        link = item.get("link", "").strip()

        combined_title = f"{title} {snippet[:300]}"
        structured_input += (
            f"<{idx}><title>{combined_title}</title><link>{link}</link></{idx}>\n"
        )
        reverse_lookup[idx] = item

    # Step 2: Call LLM with new prompt format (defined below)
    relevance_output = await call_llm_for_news_relevance(
        structured_input=structured_input, target_company=company
    )

    if isinstance(relevance_output, dict) and relevance_output.get("error") is True:
        return None

    # Step 3: Parse LLM response with source included
    pattern = r"<(\d+)>(True|False)(?:<source>(.*?)</source>)?</\1>"
    matches = re.findall(pattern, relevance_output)

    relevant_results = []
    try:
        for idx, is_relevant, source in matches:
            if is_relevant == "True":
                item = reverse_lookup[int(idx)]
                item["source"] = source.strip() if source else None
                relevant_results.append(item)
    except Exception as err:
        import traceback

        traceback.format_exc()

    if len(relevant_results) > 0:
        return relevant_results

    return None


async def fallback_yahoo_news(
    size: int,
    company_universal_name: str,
    company_display_name: Optional[str] = None,
) -> Optional[Dict]:
    if size is not None and size > 500:
        expiry_days = 7
    else:
        expiry_days = 15
    company = ""
    try:
        if company_display_name:
            company = company_display_name
            query = await generate_yahoo_query(company_display_name)
        else:
            company = company_universal_name
            query = await generate_yahoo_query(company_universal_name)

        result = await web_search_and_crawl(query, company)
        result = await filter_news_based_on_date(result)

        if result is not None:
            try:
                await experimental_cache_data(
                    key=company_universal_name,
                    value=result,
                    table="experimental_cache_company_news",
                    expiration_days=expiry_days,
                )
            except Exception as err:
                logger.error(f"Error occurred {err}")

        return result

    except Exception as e:
        return None


async def fetch_company_names_bulk(companies, client):
    company_names = [company.company_universal_name for company in companies]

    es_query = {
        "_source": [
            "li_logo",
            "li_name",
            "li_urn",
            "li_universalname",
            "li_size",
            "li_staffcount",
            "cb_stock_symbol",
        ],
        "query": {"terms": {"li_universalname": company_names}},
        "size": len(company_names),
    }

    es_response = await client.search(
        body=es_query, index=os.getenv("ES_COMPANIES_INDEX")
    )

    hits = es_response.get("hits", {}).get("hits", [])
    company_info_map = {
        hit["_source"]["li_universalname"]: {
            "li_universalname": hit["_source"].get("li_universalname"),
            "li_logo": hit["_source"].get("li_logo"),
            "li_name": hit["_source"].get("li_name"),
            "li_size": hit["_source"].get("li_size"),
            "li_staffcount": hit["_source"].get("li_staffcount"),
            "cb_stock_symbol": hit["_source"].get("cb_stock_symbol"),
            "entity_id": hit.get("_id"),  # <-- renamed to entity_id
        }
        for hit in hits
    }

    return company_info_map


def to_number(val):
    if val == "" or val is None:
        return None
    try:
        return float(val)
    except ValueError:
        return None


async def fetch_company_news_for_one_company(company, company_info, client, manticore):

    identifier = company.company_universal_name
    company_type = company.ownership_types[0] if company.ownership_types else "private"
    cache_date = company.cache_date
    company_display_name = company_info.get("li_name") if company_info else None
    company_size = company_info.get("li_size") if company_info else None
    company_staffcount = company_info.get("li_staffcount") if company_info else None

    numeric_values = filter(None, map(to_number, [company_size, company_staffcount]))
    company_size_final = max(numeric_values, default=None)

    news_count = await company_news(
        identifier=identifier,
        company_display_name=company_display_name,
        company_type=company_type,
        client=client,
        size=company_size_final,
        cache_date=cache_date,
        manticore=manticore,
        entity_urn=company.company_urn,
        entity_id=company.entity_id,
        company_universal_name=company.company_universal_name,
    )

    key = (
        company.company_universal_name
        if company.company_universal_name
        else company.company_urn
    )

    return {key: news_count}


async def company_news_generator(companies, company_info_map, es_client):
    for company in companies:
        name = company.company_universal_name
        # get company info if available else None
        company_info = company_info_map.get(name) if company_info_map else None

        try:
            # fetch news for one company only
            result = await fetch_company_news_for_one_company(
                company, company_info, es_client
            )
        except Exception as e:
            result = {
                "company_universal_name": name,
                "company_logo": None,
                "company_name": None,
                "search_engine": None,
                "company_news_details": None,
                "error": str(e),
            }

        yield json.dumps(result) + "\n"
        await asyncio.sleep(1)


async def sse_company_news_generator(companies, company_info_map, es_client):
    for company in companies:
        name = company.company_universal_name
        company_info = company_info_map.get(name) if company_info_map else None

        try:
            result = await fetch_company_news_for_one_company(
                company, company_info, es_client
            )
        except Exception as e:
            result = {
                "company_universal_name": name,
                "company_logo": None,
                "company_name": None,
                "search_engine": None,
                "company_news_details": None,
                "error": str(e),
            }

        yield f"data: {json.dumps(result)}\n\n"
        await asyncio.sleep(1)
