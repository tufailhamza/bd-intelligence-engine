import logging
from typing import List, Dict, Optional
from sqlalchemy.sql import text

logger = logging.getLogger(__name__)

from app.database.utilities import get_sqlalchemy_session_ai, fetch_person_news_db


async def fetch_person_news(
    user_id: str, table: str = "person_news"
) -> Optional[List[Dict]]:
    """
    Wrapper function to fetch person news for API usage.
    """
    try:
        return await fetch_person_news_db(table=table, user_id=user_id)
    except Exception as e:
        logger.error(
            f"Unexpected error in fetch_person_news for user_id={user_id}: {e}"
        )
        return None
