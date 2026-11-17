import os
import asyncio
import aiomysql
import logging

logger = logging.getLogger("uvicorn")


class ManticoreConnection:
    def __init__(self):
        self.pool = None

    async def get_pool(self):
        if self.pool is None:
            self.pool = await aiomysql.create_pool(
                host=os.getenv("MANTICORE_HOST", "127.0.0.1"),
                port=int(os.getenv("MANTICORE_PORT", 9306)),
                user="",
                password="",
                minsize=1,
                maxsize=int(os.getenv("MANTICORE_MAX_POOL", 10)),
            )
            logger.info("MANTICORE POOL HAS BEEN CREATED 🛸")
        return self.pool

    async def execute(self, query: str, args=None):
        try:
            pool = await self.get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(query, args)

                    # Commit if not a SELECT
                    if not query.strip().lower().startswith("select"):
                        await conn.commit()
                        logger.info("✅ Transaction committed")

                    # Only fetch for SELECT queries
                    if query.strip().lower().startswith("select"):
                        try:
                            return await cur.fetchall()
                        except Exception as e:
                            logger.error(f"Error fetching results: {e}")
                            return None
                    return None
        except Exception as e:
            logger.error(
                f"Error executing query: {query} | Args: {args} | Exception: {e}"
            )
            return None

    async def close(self):
        if self.pool:
            self.pool.close()
            await self.pool.wait_closed()
            logger.info("MANTICORE POOL HAS BEEN CLOSED 🚫")
