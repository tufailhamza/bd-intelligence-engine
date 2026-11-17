import json
import logging
from datetime import datetime, timedelta
from typing import Any, List, Optional, Tuple, Dict

from sqlalchemy.sql import text

from app.routes.dependancies import get_sqlalchemy_session_ai

logger = logging.getLogger(__name__)


async def postgres_fetch(query: str) -> Any:
    async with get_sqlalchemy_session_ai() as session:
        try:
            result = await session.execute(text(query))
            return result.fetchone()
        except Exception as e:
            logger.error(
                f"Error in fetching from postgres: {e}, Exception type: {type(e)}"
            )
        return None


async def postgres_fetch_all(query: str) -> Optional[list]:
    async with get_sqlalchemy_session_ai() as session:
        try:
            records = await session.execute(text(query))
            return records.fetchall()
        except Exception as e:
            await session.rollback()
            logger.error(
                f"Error in fetching from postgres: {e}, Exception type: {type(e)}"
            )
            return None


async def postgres_insert(query: str) -> bool:
    async with get_sqlalchemy_session_ai() as session:
        try:
            await session.execute(text(query))
            await session.commit()
            return True
        except Exception as e:
            await session.rollback()
            logger.error(
                f"Error in inserting into postgres: {e}, Exception type: {type(e)}"
            )
            return False


async def postgres_insert_tuples(query: str, params: tuple) -> bool:
    async with get_sqlalchemy_session_ai() as session:
        try:
            await session.execute(text(query), params)
            await session.commit()
        except Exception as e:
            await session.rollback()
            logger.error(
                f"Error in inserting tuples into postgres: {e}, Exception type: {type(e)}"
            )


async def postgres_delete(query: str) -> bool:
    async with get_sqlalchemy_session_ai() as session:
        try:
            await session.execute(text(query))
            await session.commit()
            return True
        except Exception as e:
            await session.rollback()
            logger.error(
                f"Error in deleting from postgres: {e}, Exception type: {type(e)}"
            )
            return False


async def delete_cached_data(key: str, table: str) -> None:
    key = key.replace("'", "''")
    async with get_sqlalchemy_session_ai() as session:
        try:
            await session.execute(text(f"DELETE FROM {table} WHERE key='{key}'"))
            await session.commit()
        except Exception as e:
            await session.rollback()
            logger.error(
                f"Error in deleting cached data from postgres: {e}, Exception type: {type(e)}"
            )


async def get_cached_data(key: str, table: str) -> Optional[Any]:
    key = key.replace("'", "''")
    async with get_sqlalchemy_session_ai() as session:
        try:
            records = await session.execute(
                text(
                    f"SELECT value, cache_date, expiration_days FROM {table} WHERE key='{key}'"
                )
            )
            record = records.fetchone()

            if record is None or len(record) == 0:
                return None

            value, cache_date, expiration_days = record

            if expiration_days == 2147483647:
                expiration_days = 3650

            expiration = False
            if cache_date and expiration_days:
                expiration_date = cache_date + timedelta(days=expiration_days)
                current_date = datetime.utcnow()
                if current_date > expiration_date:
                    expiration = True

            if expiration:
                await delete_cached_data(key, table)

            return value

        except Exception as e:
            await session.rollback()
            logger.error(
                f"Error in getting company news cached data from postgres: {e}, Exception type: {type(e)}"
            )
            return None


async def cache_data(
    key: str, value: Any, table: str, expiration_days: Optional[int] = None
) -> None:
    if expiration_days is None:
        expiration_days = 3650

    value_json = json.dumps(value)

    async with get_sqlalchemy_session_ai() as session:
        try:
            await session.execute(
                text(f"""
                    INSERT INTO {table} (key, value, cache_date, expiration_days)
                    VALUES (:key, :value, :cache_date, :expiration_days)
                    ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value, cache_date = EXCLUDED.cache_date, expiration_days = EXCLUDED.expiration_days;
                    """),
                {
                    "key": key,
                    "value": value_json,
                    "cache_date": datetime.utcnow(),
                    "expiration_days": expiration_days,
                },
            )
            await session.commit()
        except Exception as e:
            await session.rollback()
            logger.error(
                f"Error in caching company news data in postgres: {e}, Exception type: {type(e)}"
            )


# EXPERIMENTAL TABLES
async def get_experimental_cached_data(
    key: str, table: str
) -> Optional[Tuple[Any, datetime]]:
    key = key.replace("'", "''")
    async with get_sqlalchemy_session_ai() as session:
        try:
            records = await session.execute(
                text(
                    f"SELECT value, cache_date, expiration_days FROM {table} WHERE key='{key}'"
                )
            )
            record = records.fetchone()

            if record is None or len(record) == 0:
                return None

            value, cache_date, expiration_days = record

            if expiration_days == 2147483647:
                expiration_days = 3650

            expiration = False
            if cache_date and expiration_days:
                expiration_date = cache_date + timedelta(days=expiration_days)
                current_date = datetime.utcnow()
                if current_date > expiration_date:
                    expiration = True

            if expiration:
                await delete_cached_data(key, table)
                return None, cache_date

            return value, cache_date

        except Exception as e:
            await session.rollback()
            logger.error(
                f"Error in getting company news cached data from postgres: {e}, Exception type: {type(e)}"
            )
            return None


async def experimental_cache_data(
    key: str, value: Any, table: str, expiration_days: Optional[int] = None
) -> None:
    if expiration_days is None:
        expiration_days = 3650

    value_json = json.dumps(value)

    async with get_sqlalchemy_session_ai() as session:
        try:
            await session.execute(
                text(f"""
                    INSERT INTO {table} (key, value, cache_date, expiration_days)
                    VALUES (:key, :value, :cache_date, :expiration_days)
                    ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value, cache_date = EXCLUDED.cache_date, expiration_days = EXCLUDED.expiration_days;
                    """),
                {
                    "key": key,
                    "value": value_json,
                    "cache_date": datetime.utcnow(),
                    "expiration_days": expiration_days,
                },
            )
            await session.commit()
        except Exception as e:
            await session.rollback()
            logger.error(
                f"Error in caching company news data in postgres: {e}, Exception type: {type(e)}"
            )


async def get_cached_icp_profiles(
    uuid: str, table: str
) -> Optional[Tuple[dict, List[str], datetime]]:
    uuid = uuid.replace("'", "''")
    async with get_sqlalchemy_session_ai() as session:
        try:
            result = await session.execute(
                text(f"""
                    SELECT profile_ids, created_at
                    FROM {table}
                    WHERE uuid = :uuid
                """),
                {"uuid": uuid},
            )
            record = result.fetchone()
            if not record:
                return None
            return record[0], record[1]
        except Exception as e:
            logger.error(f"Error fetching cache by uuid {uuid}: {e}")
            return None


async def upsert_cached_icp_profiles(
    uuid: str, icp_json: dict, profile_ids: list, table: str
) -> None:
    async with get_sqlalchemy_session_ai() as session:
        try:
            await session.execute(
                text(f"""
                    INSERT INTO {table} (uuid, icp_json, profile_ids, created_at, last_accessed_at)
                    VALUES (:uuid, :icp_json, :profile_ids, NOW(), NOW())
                    ON CONFLICT (uuid) DO UPDATE
                    SET icp_json = EXCLUDED.icp_json,
                        profile_ids = EXCLUDED.profile_ids,
                        created_at = EXCLUDED.created_at,
                        last_accessed_at = NOW();
                """),
                {
                    "uuid": uuid,
                    "icp_json": json.dumps(icp_json),
                    "profile_ids": json.dumps(profile_ids),
                },
            )
            await session.commit()
        except Exception as e:
            await session.rollback()
            logger.error(f"Error upserting cache for uuid {uuid}: {e}")


async def update_cached_profiles_only(uuid: str, profile_ids: list, table: str) -> None:
    async with get_sqlalchemy_session_ai() as session:
        try:
            await session.execute(
                text(f"""
                    UPDATE {table}
                    SET profile_ids = :profile_ids,
                        created_at = NOW(),
                        last_accessed_at = NOW()
                    WHERE uuid = :uuid
                """),
                {
                    "uuid": uuid,
                    "profile_ids": json.dumps(profile_ids),
                },
            )
            await session.commit()
        except Exception as e:
            await session.rollback()
            logger.error(f"Error updating profile_ids for uuid {uuid}: {e}")


async def cache_person_news_db(table: str, user_id: str, posts: List[Dict]) -> None:
    """
    Insert processed person news posts into the DB.
    Args:
        table: Table name (e.g., 'person_news')
        user_id: LinkedIn user id
        posts: List of processed post dicts (already cleaned)
    """
    async with get_sqlalchemy_session_ai() as session:
        try:
            for post in posts:
                # ✅ convert string ISO → datetime.date
                date_obj = None
                if post.get("postDateISO"):
                    try:
                        date_obj = datetime.fromisoformat(post["postDateISO"]).date()
                    except ValueError:
                        # fallback if ISO parsing fails
                        date_obj = datetime.strptime(
                            post["postDateISO"], "%Y-%m-%d"
                        ).date()

                await session.execute(
                    text(f"""
                        INSERT INTO {table} 
                        (user_id, author, date, newsTitle, postText, postType, newsType)
                        VALUES (:user_id, :author, :date, :newsTitle, :postText, :postType, :newsType)
                    """),
                    {
                        "user_id": user_id,
                        "author": post["author"],
                        "date": date_obj,  # ✅ proper date
                        "newsTitle": post["newsTitle"],
                        "postText": post["postText"],
                        "postType": post["postType"],
                        "newsType": post.get("newsType"),
                    },
                )
            await session.commit()
        except Exception as e:
            await session.rollback()
            logger.error(f"Error caching person news for user_id={user_id}: {e}")
            raise


async def fetch_person_news_db(table: str, user_id: str) -> Optional[List[Dict]]:
    """
    Fetch all person news posts for a given user.
    Args:
        table: Table name (e.g., 'person_news')
        user_id: LinkedIn user id
    Returns:
        List of post dicts or None if not found
    """
    async with get_sqlalchemy_session_ai() as session:
        try:
            result = await session.execute(
                text(
                    f"SELECT post_id, user_id, author, date, newsTitle, postText, postType, newsType "
                    f"FROM {table} WHERE user_id = :user_id"
                ),
                {"user_id": user_id},
            )
            rows = result.mappings().all()
            return [dict(row) for row in rows] if rows else None
        except Exception as e:
            await session.rollback()
            logger.error(f"Error fetching person news for user_id={user_id}: {e}")
            return None
