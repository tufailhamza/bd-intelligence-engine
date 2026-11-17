"""
Fetch company documents from the ES company index for daily email (website, etc.).
"""

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Fields needed for daily-email enrichment (matches user company index shape)
COMPANY_SOURCE_FIELDS = [
    "li_universalname",
    "li_name",
    "cb_website",
    "li_website",
    "li_url",
    "li_lipage",
]


def pick_company_website(source: Optional[Dict[str, Any]]) -> Optional[str]:
    """Prefer corporate site (Crunchbase / LI website); then LinkedIn company URL."""
    if not isinstance(source, dict):
        return None
    for key in ("cb_website", "li_website"):
        v = source.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    for key in ("li_url", "li_lipage"):
        v = source.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


async def fetch_company_by_universal_names(
    es_client: Any,
    universal_names: List[str],
) -> Dict[str, Dict[str, Any]]:
    """
    Map li_universalname -> company _source (subset of fields).
    """
    if not es_client or not universal_names:
        return {}

    index = (os.getenv("ES_COMPANIES_INDEX") or "").strip()
    if not index:
        logger.warning("ES_COMPANIES_INDEX not set; skipping company website fetch")
        return {}

    ids = sorted({str(x).strip() for x in universal_names if x and str(x).strip()})
    if not ids:
        return {}

    try:
        body = {
            "query": {"terms": {"li_universalname": ids}},
            "_source": COMPANY_SOURCE_FIELDS,
            "size": len(ids),
        }
        res = await es_client.search(index=index, body=body)
    except Exception as e:
        logger.warning("fetch_company_by_universal_names failed: %s", e)
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    for hit in res.get("hits", {}).get("hits", []) or []:
        src = hit.get("_source")
        if not isinstance(src, dict):
            continue
        u = src.get("li_universalname")
        if isinstance(u, str) and u.strip():
            out[u.strip()] = dict(src)
    return out
