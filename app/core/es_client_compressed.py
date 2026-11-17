import logging
import os

from elasticsearch import AsyncElasticsearch

logger = logging.getLogger(__name__)


async def get_elasticsearch_client_with_compression():
    """Returns a compressed Elasticsearch client (http_compress=True)."""
    try:
        return AsyncElasticsearch(
            hosts=os.getenv("ES_URL"),
            api_key=(os.getenv("ES_ID"), os.getenv("ES_KEY")),
            request_timeout=30,
            max_retries=5,
            retry_on_timeout=True,
            http_compress=True,
        )
    except Exception as e:
        logger.error(f"Failed to create compressed Elasticsearch client: {e}")
        return None
