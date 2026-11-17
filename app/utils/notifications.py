import logging
import os
import httpx
import asyncio
from typing import Optional, List

logger = logging.getLogger(__name__)


async def notify_news_ingested(
    user_id: Optional[str] = None,
    company_id: Optional[str] = None,
    entity_urn: Optional[str] = None,
):
    """
    Notify that news has been ingested into Elasticsearch.

    Args:
        user_id: User public_identifier (for LinkedIn news)
        company_id: Company universal name or entity_id (for company news)
        entity_urn: Entity URN (for LinkedIn news)
    """
    m2m_route = os.getenv("M2M_ROUTE_FS", "").strip()
    m2m_bearer = os.getenv("M2M_BEARER_FS", "").strip()

    if not m2m_route or not m2m_bearer:
        logger.warning(
            "M2M_ROUTE_FS or M2M_BEARER_FS not configured, skipping notification"
        )
        return

    if not user_id and not company_id:
        logger.warning("Neither user_id nor company_id provided for notification")
        return

    payload = {}
    if user_id:
        payload["publicIdentifier"] = user_id
    if company_id:
        payload["company_id"] = company_id
    if entity_urn:
        payload["entityUrn"] = entity_urn

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url=m2m_route,
                headers={"Authorization": m2m_bearer},
                json=payload,
                timeout=10.0,
            )
            if response.status_code == 200:
                logger.info(f"Successfully notified news ingestion: {payload}")
            else:
                logger.warning(
                    f"Notification failed with status {response.status_code}: {response.text}"
                )
    except Exception as e:
        logger.error(f"Error sending news ingestion notification: {e}")


async def notify_news_ingested_batch(company_ids: List[str]):
    """
    Batch notify that news has been ingested for multiple companies.
    Sends individual notifications for each company in parallel.

    Args:
        company_ids: List of company universal names or entity_ids
    """
    if not company_ids:
        return

    m2m_route = os.getenv("M2M_ROUTE_FS", "").strip()
    m2m_bearer = os.getenv("M2M_BEARER_FS", "").strip()

    if not m2m_route or not m2m_bearer:
        logger.warning(
            "M2M_ROUTE_FS or M2M_BEARER_FS not configured, skipping batch notifications"
        )
        return

    # Create tasks for parallel notification sending
    tasks = []
    for company_id in company_ids:
        task = asyncio.create_task(notify_news_ingested(company_id=company_id))
        tasks.append(task)

    # Wait for all notifications to complete (fire-and-forget with error handling)
    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        success_count = sum(1 for r in results if not isinstance(r, Exception))
        error_count = len(results) - success_count
        if error_count > 0:
            logger.warning(
                f"Batch notification: {success_count} succeeded, {error_count} failed out of {len(company_ids)} companies"
            )
        else:
            logger.info(
                f"Batch notification: Successfully notified {success_count} companies"
            )
