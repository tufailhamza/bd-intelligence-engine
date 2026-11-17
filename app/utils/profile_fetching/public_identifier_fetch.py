import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from elasticsearch import AsyncElasticsearch

logger = logging.getLogger(__name__)


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        # Handle trailing "Z" as UTC offset for fromisoformat
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _select_top_experience(experiences: List[Any]) -> Optional[Dict[str, Any]]:
    """
    Pick the "top/most relevant" experience entry.
    Heuristics:
      1) Lowest numeric `index` (many datasets store most recent as index=0)
      2) Current role (`end` is None), prefer latest `start`
      3) Latest `start`
      4) Fallback to first dict entry
    """
    exp_dicts: List[Dict[str, Any]] = [e for e in experiences if isinstance(e, dict)]
    if not exp_dicts:
        return None

    indexed = [e for e in exp_dicts if isinstance(e.get("index"), int)]
    if indexed:
        return min(indexed, key=lambda e: e.get("index"))

    current = [e for e in exp_dicts if e.get("end") in (None, "", "null")]
    if current:
        return max(
            current, key=lambda e: _parse_iso_datetime(e.get("start")) or datetime.min
        )

    with_start = [
        e for e in exp_dicts if _parse_iso_datetime(e.get("start")) is not None
    ]
    if with_start:
        return max(
            with_start,
            key=lambda e: _parse_iso_datetime(e.get("start")) or datetime.min,
        )

    return exp_dicts[0]


async def fetch_profile_by_public_identifier(
    public_identifier: str,
    es_client: AsyncElasticsearch,
    source_fields: Optional[List[str]] = None,
    only_top_experience: bool = True,
    entity_urn: Optional[str] = None,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    Fetch a single profile document from Elasticsearch by its `public_identifier`
    or stable `entity_urn`.

    Returns:
        (es_id, profile_source) where either can be None if not found.
    """
    index = os.getenv("ES_PROFILES_INDEX")
    if not index:
        raise RuntimeError("ES_PROFILES_INDEX env var is not set")

    public_identifier = (public_identifier or "").strip()
    entity_urn = (entity_urn or "").strip()
    should: List[Dict[str, Any]] = []
    if public_identifier:
        should.extend(
            [
                {"term": {"public_identifier": public_identifier}},
            ]
        )
    if entity_urn:
        should.extend([{"term": {"entity_urn": entity_urn}}])

    if not should:
        return None, None

    body: Dict[str, Any] = {
        "query": {
            "bool": {
                "should": should,
                "minimum_should_match": 1,
            }
        },
        "size": 1,
    }

    if source_fields is not None:
        body["_source"] = source_fields

    res = await es_client.search(index=index, body=body)
    hits = res.get("hits", {}).get("hits", []) or []
    if not hits:
        logger.warning(
            "No hits found for public_identifier=%s entity_urn=%s in index=%s",
            public_identifier,
            entity_urn,
            index,
        )
        return None, None

    hit = hits[0]
    es_id = hit.get("_id")
    profile_source = hit.get("_source")

    logger.info(
        "ES hit for public_identifier=%s entity_urn=%s: es_id=%s, has_source=%s, hit_keys=%s",
        public_identifier,
        entity_urn,
        es_id if es_id else "None",
        "yes" if profile_source else "no",
        list(hit.keys()) if isinstance(hit, dict) else "not_a_dict",
    )

    if isinstance(profile_source, dict) and only_top_experience:
        # Avoid mutating ES client's internal structures / shared dicts
        profile_source = dict(profile_source)
        exp = profile_source.get("experience")
        if isinstance(exp, list) and exp:
            top = _select_top_experience(exp)
            profile_source["experience"] = [top] if top is not None else []

    return es_id, profile_source


async def fetch_profile_by_es_id(
    es_id: str,
    es_client: AsyncElasticsearch,
    source_fields: Optional[List[str]] = None,
    only_top_experience: bool = True,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    Fetch a single profile document from Elasticsearch by its document `_id`.

    Returns:
        (es_id, profile_source) where either can be None if not found.
    """
    index = os.getenv("ES_PROFILES_INDEX")
    if not index:
        raise RuntimeError("ES_PROFILES_INDEX env var is not set")

    identifier = (es_id or "").strip()
    if not identifier:
        return None, None

    try:
        kwargs: Dict[str, Any] = {"index": index, "id": identifier}
        if source_fields is not None:
            kwargs["_source"] = source_fields
        res = await es_client.get(**kwargs)
    except Exception:
        logger.warning(
            "No profile found for es_id=%s in index=%s",
            identifier,
            index,
            exc_info=True,
        )
        return None, None

    profile_source = res.get("_source")
    if not profile_source:
        logger.warning(
            "No profile source found for es_id=%s in index=%s",
            identifier,
            index,
            exc_info=True,
        )
        return identifier, None

    logger.info(
        "ES hit for es_id=%s: has_source=%s",
        identifier,
        "yes" if profile_source else "no",
    )

    if only_top_experience:
        profile_source = dict(profile_source)
        exp = profile_source.get("experience")
        if isinstance(exp, list) and exp:
            top = _select_top_experience(exp)
            profile_source["experience"] = [top] if top is not None else []

    return identifier, profile_source


async def fetch_profiles_by_public_identifiers(
    public_identifiers: List[str],
    es_client: AsyncElasticsearch,
    source_fields: Optional[List[str]] = None,
    only_top_experience: bool = True,
) -> Dict[str, Tuple[Optional[str], Optional[Dict[str, Any]]]]:
    """
    Fetch multiple profile documents from Elasticsearch by their `public_identifier`s in a single query.

    Args:
        public_identifiers: List of public identifiers to fetch
        es_client: AsyncElasticsearch client
        source_fields: Optional list of fields to return
        only_top_experience: If True, only return the top experience for each profile

    Returns:
        Dictionary mapping public_identifier -> (es_id, profile_source)
        Missing profiles will have (None, None) as values
    """
    if not public_identifiers:
        return {}

    index = os.getenv("ES_PROFILES_INDEX")
    if not index:
        raise RuntimeError("ES_PROFILES_INDEX env var is not set")

    body: Dict[str, Any] = {
        "query": {"terms": {"public_identifier": public_identifiers}},
        "size": len(public_identifiers),
    }

    if source_fields is not None:
        body["_source"] = source_fields

    res = await es_client.search(index=index, body=body)
    hits = res.get("hits", {}).get("hits", []) or []

    # Create a mapping of public_identifier -> (es_id, profile_source)
    result_map: Dict[str, Tuple[Optional[str], Optional[Dict[str, Any]]]] = {}

    # Initialize all identifiers with None values
    for pid in public_identifiers:
        result_map[pid] = (None, None)

    # Process hits and map them by public_identifier
    for hit in hits:
        profile_source = hit.get("_source")
        if not isinstance(profile_source, dict):
            continue

        public_id = profile_source.get("public_identifier")
        if not public_id:
            continue

        es_id = hit.get("_id")

        # Process experience if needed
        if isinstance(profile_source, dict) and only_top_experience:
            # Avoid mutating ES client's internal structures / shared dicts
            profile_source = dict(profile_source)
            exp = profile_source.get("experience")
            if isinstance(exp, list) and exp:
                top = _select_top_experience(exp)
                profile_source["experience"] = [top] if top is not None else []

        result_map[public_id] = (es_id, profile_source)

    return result_map
