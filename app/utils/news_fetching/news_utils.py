import asyncio
import base64
import json
import logging
import os
import re
import time
import string
import textwrap
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import dateparser
import feedparser
import httpx
from bs4 import BeautifulSoup
from fastapi import HTTPException

logger = logging.getLogger(__name__)


def _google_news_html_is_bot_challenge_shell(html: str) -> bool:
    """
    True when the body looks like Google's JS / consent / search-guard shell,
    not a rendered news SERP (no Gx5Zad blocks will be present).
    """
    if not isinstance(html, str) or len(html) < 200:
        return False
    if "/httpservice/retry/enablejs" in html:
        return True
    if "emsg=SG_REL" in html or "&emsg=SG_REL&amp;" in html:
        return True
    if "cad=sg_trbl" in html and "sg_b_e" in html:
        return True
    return False


PRIMARY_KEYWORDS = [
    "funding",
    "IPO",
    "hiring",
    "executive",
    "recruitment",
    "acquisition",
    "merger",
    "buyout",
    "expansion",
    "partnership",
    "CEO",
    "CFO",
    "earnings",
    "quarterly results",
    "departure",
    "vacancies",
    "layoffs",
    "restructuring",
    "strategy",
    "product launch",
    "board appointment",
    "new product",
]

SECONDARY_KEYWORDS = [
    "financial results",
    "regulation",
    "litigation",
    "settlement",
    "closure",
    "opening",
    "venture",
    "alliance",
    "competition",
    "disruption",
    "contract",
    "loss",
    "technology",
    "crisis",
    "contract",
    "opening",
    "instability",
    "transition",
    "uncertainty",
]


def _coerce_cached_date_str(cached_date: Any) -> str:
    """Normalize to YYYY-MM-DD for Google `after:` filters."""
    if cached_date is None:
        return datetime.today().date().isoformat()
    try:
        if isinstance(cached_date, str):
            return datetime.fromisoformat(cached_date).date().isoformat()
        if isinstance(cached_date, datetime):
            return cached_date.date().isoformat()
        if isinstance(cached_date, date):
            return cached_date.isoformat()
    except Exception:
        pass
    return datetime.today().date().isoformat()


def build_google_news_rss_url(
    keywords: list, company_name: str, cached_date: Any
) -> str:
    """
    Google News RSS search (same query shape as web `tbm=nws`, usually no Pegasus/HTML scrape).
    """
    when = _coerce_cached_date_str(cached_date)
    quoted_keywords = [f'"{kw}"' if " " in kw else kw for kw in keywords]
    keyword_part = " OR ".join(quoted_keywords)
    full_query = f'intitle:"{company_name}" after:{when} ({keyword_part})'
    encoded_query = urllib.parse.quote_plus(full_query)
    return (
        f"https://news.google.com/rss/search?q={encoded_query}"
        f"&hl=en-US&gl=US&ceid=US:en"
    )


def _published_iso_naive_for_valid_entry(raw: Optional[str]) -> Optional[str]:
    """Match `is_valid_entry` ISO regex (naive, no timezone suffix)."""
    if not raw or not str(raw).strip():
        return None
    try:
        dt = dateparser.parse(
            raw.strip(),
            settings={"RELATIVE_BASE": datetime.now(), "PREFER_DATES_FROM": "past"},
        )
        if not dt:
            return None
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return None


def _rss_entry_to_news_dict(entry: Any) -> Optional[Dict[str, Any]]:
    title_raw = (entry.get("title") or "").strip()
    link = (entry.get("link") or "").strip()
    if not title_raw or not link:
        return None
    summary_html = entry.get("summary") or entry.get("description") or ""
    snippet = ""
    if summary_html:
        snippet = BeautifulSoup(summary_html, "html.parser").get_text(
            separator=" ", strip=True
        )
    if not snippet:
        snippet = title_raw[:500]

    source = ""
    src = entry.get("source")
    if isinstance(src, dict) and (src.get("title") or "").strip():
        source = str(src["title"]).strip()
    title = title_raw
    if not source and " - " in title_raw:
        left, right = title_raw.rsplit(" - ", 1)
        if len(right) <= 120:
            title, source = left.strip(), right.strip()
    if not source:
        source = "Unknown"

    pub_raw = entry.get("published") or entry.get("updated") or ""
    published_date = _published_iso_naive_for_valid_entry(pub_raw)
    if not published_date and entry.get("published_parsed"):
        try:
            pub_raw = time.strftime("%Y-%m-%d %H:%M:%S", entry.published_parsed)
            published_date = _published_iso_naive_for_valid_entry(pub_raw)
        except Exception:
            published_date = None
    if not published_date:
        return None

    return {
        "title": title,
        "link": link,
        "snippet": snippet,
        "published_date": published_date,
        "source": source,
    }


async def _fetch_google_news_rss_payload(
    *,
    company_query: str,
    keywords: List[str],
    cached_date: Any,
) -> List[Dict[str, Any]]:
    if not company_query.strip():
        return []
    url = build_google_news_rss_url(keywords, company_query.strip(), cached_date)
    logger.debug("Google News RSS URL: %s", url[:500])
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (compatible; BDNewsIngest/1.0; +https://news.google.com/)"
                    ),
                    "Accept": "application/rss+xml, application/xml, text/xml, */*",
                },
            )
            resp.raise_for_status()
            parsed = feedparser.parse(resp.text)
    except Exception as e:
        logger.warning("Google News RSS request/parse failed: %s", e)
        return []

    if getattr(parsed, "bozo", False) and not (parsed.entries or []):
        logger.warning(
            "Google News RSS marked bozo=%s: %s",
            getattr(parsed, "bozo_exception", None),
            url[:200],
        )

    out: List[Dict[str, Any]] = []
    for entry in parsed.entries or []:
        row = _rss_entry_to_news_dict(entry)
        if row:
            out.append(row)
    return out


def build_google_news_url(
    keywords: list,
    company_name: str,
    cb_stock_symbol: str,
    li_website: str,
    cached_date: str,
) -> str:
    cached_date = _coerce_cached_date_str(cached_date)

    """
    Build a Google News tab search URL with the query structured as:
    intitle:company_name after:cached_date keywords ORed without quotes or parentheses.

    Parameters:
    - company_name: Name of the company (searched in title)
    - cb_stock_symbol: Stock symbol (currently unused but may be used for logging/debugging)
    - li_website: LinkedIn or official website (currently unused, reserved for future enhancements)
    - cached_date: Filter results after this date (format: YYYY-MM-DD)

    Returns:
    - A formatted Google News search URL
    """
    try:

        # Quote multi-word keywords, leave single words as-is
        quoted_keywords = [f'"{kw}"' if " " in kw else kw for kw in keywords]
        keyword_part = " OR ".join(quoted_keywords)

        # Build the full query string
        full_query = f'intitle:"{company_name}" after:{cached_date} ({keyword_part})'
        encoded_query = urllib.parse.quote_plus(full_query)

        url = f"https://www.google.com/search?q={encoded_query}&tbm=nws"

        logger.debug(f"Constructed Google News URL for company '{company_name}': {url}")
        return url

    except Exception as e:
        logger.error(
            f"Error building Google News URL for '{company_name}': {e}", exc_info=True
        )
        raise HTTPException(
            status_code=500, detail="Failed to construct Google News URL."
        )


def parse_published_date(date_str: str) -> str:
    """
    Parses a wide variety of date strings and returns an ISO 8601 string.
    If parsing fails, returns the original string.

    Parameters:
    - date_str: The input date string in natural language or various formats.

    Returns:
    - A string in ISO 8601 format if successfully parsed, otherwise the original input string.
    """
    try:
        parsed_date = dateparser.parse(
            date_str,
            settings={"RELATIVE_BASE": datetime.now(), "PREFER_DATES_FROM": "past"},
        )
        if parsed_date:
            return parsed_date.isoformat()
        else:
            logger.warning(f"Unable to parse date string (returned None): '{date_str}'")
            return date_str
    except Exception as e:
        logger.error(
            f"Exception while parsing date string '{date_str}': {e}", exc_info=True
        )
        return date_str


async def get_source_from_pegasus(params):
    """
    Fetches data from the Pegasus service using provided query parameters.

    Returns:
    - A parsed JSON response dict on success.
    - A standardized error dict on failure.
    """
    pegasus_url = os.getenv("CLOUDFUNCTION_SERVICE")
    headers = {"accept": "application/json"}

    async with httpx.AsyncClient(timeout=300) as client:
        try:
            response = await client.get(pegasus_url, headers=headers, params=params)
            response.raise_for_status()
            json_response = response.json()
            return json_response

        except httpx.HTTPStatusError as http_err:
            status_code = http_err.response.status_code
            message = f"HTTP error occurred: {http_err.response.text}"
            logger.warning(f"Pegasus HTTPStatusError {status_code} - {message}")
            return {
                "error": True,
                "type": "HTTPStatusError",
                "status_code": status_code,
                "message": message,
            }

        except httpx.RequestError as req_err:
            message = f"Request error occurred: {str(req_err)}"
            logger.error(f"Pegasus RequestError - {message}")
            return {"error": True, "type": "RequestError", "message": message}

        except Exception as e:
            message = f"Unexpected exception: {str(e)}"
            logger.exception(f"Pegasus unknown exception - {message}")
            return {"error": True, "type": "Exception", "message": message}


async def fetch_news(
    cached_date,
    company_name=None,
    identifier=None,
    li_website=None,
    num_retries=3,
    cb_stock_symbol=None,
    keywords=None,
    count=0,
):
    """
    Fetch news articles related to a given company or identifier by scraping
    Google News search results.

    This function constructs a Google News URL based on the provided identifier or
    stock symbol and attempts to retrieve news data through Pegasus source fetching.
    It retries fetching up to `num_retries` times if necessary.

    After fetching the raw HTML, it parses the news blocks to extract relevant
    information such as title, link, snippet, published date, and source.

    Parameters:
        cached_date (str): Date string used to filter or cache news results.
        company_name (str, optional): The company name (not used in URL building here).
        identifier (str, optional): The identifier used for constructing the news URL.
        li_website (str, optional): LinkedIn or other website context for the search.
        num_retries (int, optional): Number of retry attempts for fetching news data. Defaults to 3.
        cb_stock_symbol (str, optional): Stock symbol to use in the news search URL.

    Returns:
        list[dict] or None: A list of dictionaries containing news data with keys:
            - "title": Title of the news article.
            - "link": URL to the news article.
            - "snippet": Short snippet or summary of the news.
            - "published_date": Parsed publication date of the article.
            - "source": Source or publisher of the news.
        Returns None if no news data is found
        Returns error if error occurs during fetching news or parsing.
    """
    if not keywords:
        keywords = PRIMARY_KEYWORDS

    news_priority = []
    news_priority.append(
        build_google_news_url(
            keywords,
            company_name=identifier,
            cb_stock_symbol=cb_stock_symbol,
            li_website=li_website,
            cached_date=cached_date,
        )
    )

    result: Any = None
    for iteration in range(num_retries):
        if not news_priority:
            break
        link = news_priority.pop(0)
        response = await get_source_from_pegasus(params={"link": link})

        if isinstance(response, dict) and response.get("error"):
            return {"error": True}

        if response and "link_result" in response:
            result = response["link_result"]

    if result is None:
        logger.error("Pegasus returned no link_result after fetch attempts")
        return {"error": True}

    try:
        soup = BeautifulSoup(result, "html.parser")
    except Exception as e:
        logger.error(f"Error parsing HTML with BeautifulSoup: {e}")
        return {"error": True}

    results_data = []

    # Try new structure first (Google News current format)
    news_blocks = soup.find_all("div", class_="Gx5Zad xpd EtOod pkphOe")

    if news_blocks:
        for block in news_blocks:
            try:
                # Extract URL from parent <a> tag
                a_tag = block.find_parent("a", href=True)
                if not a_tag:
                    a_tag = block.find("a", href=True)

                url = None
                if a_tag:
                    href = a_tag.get("href", "")
                    # Extract actual URL from Google redirect URL
                    if "/url?" in href:
                        parsed = urllib.parse.urlparse(href)
                        query_params = urllib.parse.parse_qs(parsed.query)
                        url = query_params.get("url", [None])[0]
                    else:
                        url = href

                # Extract title from div with class "UFvD1"
                title = None
                title_div = block.find("div", class_="UFvD1")
                if title_div:
                    title = title_div.get_text(strip=True)

                # Extract snippet from div with class "H66NU"
                snippet = None
                snippet_div = block.find("div", class_="H66NU")
                if snippet_div:
                    snippet = snippet_div.get_text(strip=True)

                # Extract date from span with class "UK5aid MDvRSc"
                date = None
                date_span = block.find("span", class_="UK5aid MDvRSc")
                if date_span:
                    date_text = date_span.get_text(strip=True)
                    # Parse relative dates like "12 hours ago", "1 day ago", etc.
                    date = parse_published_date(date_text)

                # Extract source from div with class "BamJPe"
                source = None
                source_div = block.find("div", class_="BamJPe")
                if source_div:
                    source = source_div.get_text(strip=True)

                # Store result
                if title and url:
                    results_data.append(
                        {
                            "title": title,
                            "link": url,
                            "snippet": snippet,
                            "published_date": date,
                            "source": source,
                        }
                    )

            except Exception as e:
                logger.debug(f"Error parsing news block: {e}")
                continue

    # Fallback to old structure if no results found
    if not results_data:
        for block in soup.find_all(attrs={"data-hveid": True}):
            if not (
                block.has_attr("data-news-cluster-id")
                or block.has_attr("data-news-doc-id")
            ):
                continue

            try:
                # Extract URL from <a> tag
                a_tag = block.find("a", href=True)
                url = a_tag["href"] if a_tag else None

                # Extract title from specific div with role="heading"
                title = None
                title_div = block.find("div", {"role": "heading"})
                if title_div:
                    title = title_div.get_text(strip=True)

                # Extract snippet from div with class "GI74Re nDgy9d"
                snippet = None
                snippet_div = block.find("div", class_="GI74Re nDgy9d")
                if snippet_div:
                    snippet = snippet_div.get_text(strip=True)

                # Extract date from span with data-ts attribute
                date = None
                date_span = block.find("span", {"data-ts": True})
                if date_span:
                    # Convert timestamp to ISO format
                    timestamp = int(date_span.get("data-ts"))
                    date_obj = datetime.fromtimestamp(timestamp)
                    date = date_obj.isoformat()

                # Extract source from span within div with class "MgUUmf NUnG9d"
                source = None
                source_div = block.find("div", class_="MgUUmf NUnG9d")
                if source_div:
                    source_span = source_div.find("span")
                    if source_span:
                        source = source_span.get_text(strip=True)

                # Store result
                if title and url:
                    results_data.append(
                        {
                            "title": title,
                            "link": url,
                            "snippet": snippet,
                            "published_date": date,
                            "source": source,
                        }
                    )

            except Exception:
                continue

    if not results_data and isinstance(result, str):
        if _google_news_html_is_bot_challenge_shell(result):
            logger.warning(
                "Google HTML is a bot/consent challenge shell (enablejs / SG_REL / search-guard), "
                "not a news SERP. Selectors (Gx5Zad, data-news-*) will not match; "
                "ingestion needs rendered JS, different cookies, or a non-scrape source."
            )
        elif "Gx5Zad" not in result and "data-news-" not in result:
            logger.warning(
                "No known Google News SERP markers (Gx5Zad / data-news-*) in HTML; "
                "page may be CAPTCHA, region block, or an unsupported layout."
            )

    iso8601_regex = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$")

    def is_valid_entry(entry):
        # Check required fields are non-empty (including title)
        if not all(
            entry.get(field) not in (None, "")
            for field in ["title", "link", "snippet", "source", "published_date"]
        ):
            return False

        # Check published_date format (should be ISO format now)
        if not iso8601_regex.match(entry.get("published_date", "")):
            return False

        # Only check if title is mostly English
        if not is_english(entry.get("title", "")):
            return False

        return True

    filtered_results = list(filter(is_valid_entry, results_data))
    results_data = filtered_results

    rss_flag = os.getenv("GOOGLE_NEWS_RSS_FALLBACK", "1").strip().lower()
    if not results_data and rss_flag not in ("0", "false", "no", "off"):
        company_q = (identifier or company_name or "").strip()
        if company_q:
            rss_rows = await _fetch_google_news_rss_payload(
                company_query=company_q,
                keywords=keywords,
                cached_date=cached_date,
            )
            results_data = list(filter(is_valid_entry, rss_rows))
            if results_data:
                logger.info(
                    "Google News RSS fallback: %s item(s) after scrape had none",
                    len(results_data),
                )

    if results_data:
        return results_data

    if count == 0:
        logger.info(
            "Primary keywords returned no results. Switching to secondary keywords."
        )
        return await fetch_news(
            cached_date=cached_date,
            company_name=company_name,
            identifier=identifier,
            li_website=li_website,
            num_retries=num_retries,
            cb_stock_symbol=cb_stock_symbol,
            keywords=SECONDARY_KEYWORDS,
            count=1,
        )
    else:
        return []


def is_english(text, threshold=0.7):
    if not text:
        return False
    ascii_chars = sum(1 for c in text if c in string.printable)
    return (ascii_chars / len(text)) >= threshold


# UNUSED FUNCTIONS IN CURRENT CODE BUT MIGHT BE IMPORTED SOMEWHERE ALTHOUGH NOT USED SO NOT COMMENTING


async def extract_original_link(payload):
    tasks = []
    for element in payload:
        url = element.get("image_url")
        task = extract_url(url)
        tasks.append(task)

    results = await asyncio.gather(*tasks)
    for ind, element in enumerate(payload):
        if results[ind]:
            element["image_url"] = str(results[ind])
        else:
            element["image_url"] = None

    return payload


def is_news_recent(news_array):
    one_year_ago = datetime.now() - timedelta(days=365)
    old_news_count = 0

    for news in news_array:
        published_date = datetime.fromisoformat(news["published_date"].replace("Z", ""))
        if published_date < one_year_ago:
            old_news_count += 1
    return old_news_count < len(news_array) / 5


async def extract_url(url):
    if not url:
        return None
    payload_base_64 = None
    pegasus_response = None
    code_string = textwrap.dedent(f"""
        import httpx

        def fetch_url(url):
            try:
                headers = {{
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                    'Referer': 'https://www.google.com/'
                }}
                with httpx.Client(follow_redirects=True) as client:
                    response = client.get(url, headers=headers, timeout=10)
                    response.raise_for_status()
                    final_url = str(response.url) 
                    return {{
                        "final_url": final_url
                    }}
            except httpx.RequestError as e:
                return str(e)

        result = fetch_url("{url}")
    """)
    payload = {"custom_function": {"code": code_string, "imports": ["httpx"]}}
    payload_base_64 = json_to_base64(payload)
    pegasus_response = await get_source_from_pegasus(
        params={"custom_function": payload_base_64}
    )
    try:
        if "code_result" in pegasus_response:
            if "result" in pegasus_response["code_result"]:
                if "final_url" in pegasus_response["code_result"]["result"]:
                    return pegasus_response["code_result"]["result"]["final_url"]
    except:
        return None

    return None


def json_to_base64(data):
    json_str = json.dumps(data)
    base64_bytes = base64.b64encode(json_str.encode("utf-8"))
    base64_str = base64_bytes.decode("utf-8")
    return base64_str


async def filter_news_based_on_date(
    news_list: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if news_list is None:
        return []

    filtered = []
    now = datetime.utcnow()  # naive UTC datetime
    sixty_days_ago = now - timedelta(days=60)

    for item in news_list:
        try:
            date_str = item["published_date"]

            if date_str.endswith("Z"):
                # Case 2: convert 'Z' to naive datetime string format
                dt_aware = datetime.fromisoformat(
                    date_str[:-1] + "+00:00"
                )  # aware datetime UTC
                dt_naive = dt_aware.replace(tzinfo=None)  # convert to naive UTC
                if dt_naive >= sixty_days_ago:
                    # Update the published_date string in naive format without timezone
                    item["published_date"] = dt_naive.isoformat()
                    filtered.append(item)

            else:
                # Case 1: naive datetime string expected
                dt_naive = datetime.fromisoformat(date_str)
                if dt_naive >= sixty_days_ago:
                    filtered.append(item)

        except (ValueError, KeyError, TypeError) as err:
            # Case 3: invalid format, skip
            logger.info(f"Skipping invalid date: {err}")
            continue

    return filtered
