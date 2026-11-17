import argparse
import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv
from elasticsearch import AsyncElasticsearch
from elasticsearch.helpers import async_bulk, async_scan

from app.utils.news_semantics import (
    NEWS_SEMANTIC_DIMENSIONS,
    NEWS_SEMANTIC_MODEL,
    enrich_news_docs_with_semantics,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


SOURCE_FIELDS = [
    "id",
    "news_id",
    "linkedin_identifier",
    "entity_urn",
    "entity_id",
    "entity_type",
    "company_name",
    "author",
    "author_description",
    "last_author_name",
    "last_author_description",
    "news_title",
    "content",
    "link",
    "source",
    "published_date",
    "published_date_relative",
    "post_type",
    "news_type",
    "news_sub_type",
    "cache_date",
    "expiration_days",
    "original_author_linkedin_identifier",
    "original_author_entity_id",
    "original_author_entity_urn",
    "repost_original_entity_type",
    "imagecount",
    "videocount",
]


async def ensure_semantic_mapping(client: AsyncElasticsearch, index: str) -> None:
    await client.indices.put_mapping(
        index=index,
        properties={
            "semantic_text": {"type": "text", "analyzer": "standard"},
            "semantic_embedding": {
                "type": "dense_vector",
                "dims": NEWS_SEMANTIC_DIMENSIONS,
                "index": True,
                "similarity": "cosine",
            },
            "semantic_model": {"type": "keyword"},
            "semantic_created_at": {"type": "date"},
            "event_priority_score": {"type": "float"},
            "event_priority_label": {"type": "keyword"},
        },
    )


def missing_semantics_query() -> Dict[str, Any]:
    return {
        "bool": {
            "must_not": [{"exists": {"field": "semantic_embedding"}}],
        }
    }


async def count_missing(client: AsyncElasticsearch, index: str) -> int:
    res = await client.count(index=index, query=missing_semantics_query())
    return int(res.get("count") or 0)


async def flush_batch(
    *,
    client: AsyncElasticsearch,
    index: str,
    batch: List[Dict[str, Any]],
) -> int:
    if not batch:
        return 0

    docs = [dict[Any, Any](item["_source"]) for item in batch]
    enriched = await enrich_news_docs_with_semantics(docs)
    actions = []
    for hit, doc in zip(batch, enriched):
        embedding = doc.get("semantic_embedding")
        if not isinstance(embedding, list) or not embedding:
            raise RuntimeError(
                "Embedding generation failed; aborting backfill before bulk update. "
                "Check OPENAI_API_KEY / embedding provider credentials."
            )
        actions.append(
            {
                "_op_type": "update",
                "_index": index,
                "_id": hit["_id"],
                "doc": {
                    "semantic_text": doc.get("semantic_text"),
                    "semantic_embedding": embedding,
                    "semantic_model": doc.get("semantic_model"),
                    "semantic_created_at": doc.get("semantic_created_at"),
                    "event_priority_score": doc.get("event_priority_score"),
                    "event_priority_label": doc.get("event_priority_label"),
                },
            }
        )

    success, errors = await async_bulk(client, actions, raise_on_error=False)
    if errors:
        logger.error("Bulk update errors (first 3): %s", errors[:3])
    return int(success)


def _format_seconds(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _progress_message(
    *,
    updated: int,
    total: int,
    started_at: float,
    batch_no: int,
) -> str:
    elapsed = time.monotonic() - started_at
    rate = (updated / elapsed) if elapsed > 0 and updated > 0 else 0.0
    remaining = max(0, total - updated)
    eta = (remaining / rate) if rate > 0 else 0.0
    percent = (updated / total * 100.0) if total > 0 else 100.0
    return (
        f"batch={batch_no} updated={updated}/{total} "
        f"({percent:.1f}%) elapsed={_format_seconds(elapsed)} "
        f"rate={rate:.2f} docs/s eta={_format_seconds(eta)}"
    )


async def run(args: argparse.Namespace) -> None:
    env_path = Path(args.env_file)
    if env_path.exists():
        load_dotenv(env_path, override=True)
    else:
        load_dotenv(override=True)

    url = (os.getenv("ES_URL_BD") or "").strip()
    api_key = (os.getenv("ES_API_KEY_BD") or "").strip()
    index = args.index or (os.getenv("ES_BD_NEWS_INDEX") or "bd_news").strip()
    if not url or not api_key:
        raise RuntimeError("ES_URL_BD and ES_API_KEY_BD are required")

    client = AsyncElasticsearch(hosts=[url], api_key=api_key, request_timeout=60)
    try:
        missing = await count_missing(client, index)
        logger.info(
            "bd_news semantic backfill: index=%s missing_semantic_embedding=%d model=%s",
            index,
            missing,
            NEWS_SEMANTIC_MODEL,
        )
        if args.dry_run or missing == 0:
            return

        await ensure_semantic_mapping(client, index)
        logger.info(
            "Starting semantic backfill: index=%s total=%d batch_size=%d model=%s",
            index,
            missing,
            args.batch_size,
            NEWS_SEMANTIC_MODEL,
        )

        query = {
            "query": missing_semantics_query(),
            "_source": SOURCE_FIELDS,
        }
        batch: List[Dict[str, Any]] = []
        total = 0
        batch_no = 0
        started_at = time.monotonic()
        async for hit in async_scan(
            client,
            index=index,
            query=query,
            size=args.batch_size,
            preserve_order=False,
            request_timeout=60,
        ):
            batch.append(hit)
            if len(batch) >= args.batch_size:
                batch_no += 1
                logger.info(
                    "Starting batch=%d size=%d progress_before=%d/%d",
                    batch_no,
                    len(batch),
                    total,
                    missing,
                )
                updated = await flush_batch(client=client, index=index, batch=batch)
                total += updated
                logger.info(
                    "Finished batch=%d updated_in_batch=%d %s",
                    batch_no,
                    updated,
                    _progress_message(
                        updated=total,
                        total=missing,
                        started_at=started_at,
                        batch_no=batch_no,
                    ),
                )
                batch = []

        if batch:
            batch_no += 1
            logger.info(
                "Starting final batch=%d size=%d progress_before=%d/%d",
                batch_no,
                len(batch),
                total,
                missing,
            )
            updated = await flush_batch(client=client, index=index, batch=batch)
            total += updated
            logger.info(
                "Finished final batch=%d updated_in_batch=%d %s",
                batch_no,
                updated,
                _progress_message(
                    updated=total,
                    total=missing,
                    started_at=started_at,
                    batch_no=batch_no,
                ),
            )
        logger.info(
            "Backfill complete. %s",
            _progress_message(
                updated=total,
                total=missing,
                started_at=started_at,
                batch_no=batch_no,
            ),
        )
    finally:
        await client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill bd_news semantic fields.")
    parser.add_argument("--index", default=None)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--env-file", default=".env")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
