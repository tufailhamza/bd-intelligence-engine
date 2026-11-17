import traceback
import logging
import uuid
import datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.utils.person_news.person_news_fetching import fetch_person_news

logger = logging.getLogger(__name__)

router = APIRouter()


def clean_for_json(obj):
    """Convert non-JSON serializable types to str."""
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, (datetime.date, datetime.datetime)):  # ⬅️ add this
        return obj.isoformat()
    return obj


@router.post("/fetch_linkedin_news")
async def fetch_linkedin_news(data: dict):
    try:
        user_id = data.get("user_id")
        if not user_id:
            return JSONResponse(
                content={"success": False, "error": "Missing user_id"},
                status_code=400,
            )

        # fetch person news from DB
        news_posts = await fetch_person_news(user_id=user_id, table="person_news")

        if news_posts is None:
            return JSONResponse(content={"success": True, "data": []}, status_code=200)

        # ✅ Clean UUIDs (or other types) before returning
        safe_posts = [
            {k: clean_for_json(v) for k, v in post.items()} for post in news_posts
        ]

        return JSONResponse(content={"success": True, "data": safe_posts})

    except Exception as e:
        logger.error(f"Error in fetch_linkedin_news endpoint: {e}")
        traceback.print_exc()
        return JSONResponse(
            content={"success": False, "error": str(e)}, status_code=500
        )
