import asyncio
import logging
import os
from asyncio import current_task
from typing import Callable

from elasticsearch import ApiError
from elasticsearch import AsyncElasticsearch
from fastapi import FastAPI, Request
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_scoped_session,
    create_async_engine,
)
from sqlalchemy.orm import sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.es_client_compressed import get_elasticsearch_client_with_compression
from qutils.database.elastic_search import ElasticsearchConnection
from qutils.database.post_gres import DatabaseConnection
from app.core.manticore_client import ManticoreConnection

logger = logging.getLogger("uvicorn")


class ElasticsearchMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        app = request.app
        if not hasattr(app.state, "elasticsearch") or app.state.elasticsearch is None:
            await self._reinitialize_es(app)

        try:
            await app.state.elasticsearch.ping()
        except ApiError:
            logger.warning("Elasticsearch client is unavailable. Reconnecting...")
            await self._reinitialize_es(app)

        response = await call_next(request)
        return response

    async def _reinitialize_es(self, app: FastAPI):
        es_connection = ElasticsearchConnection()
        app.state.elasticsearch = await es_connection.get_elasticsearch_client()
        logger.info("ELASTICSEARCH CLIENT HAS BEEN RE-INSTANTIATED 🛸")

        app.state.elasticsearch_compressed = (
            await get_elasticsearch_client_with_compression()
        )
        logger.info("ELASTICSEARCH CLIENT WITH COMPRESSION HAS BEEN RE-INSTANTIATED")


# class PostgresMiddleware(BaseHTTPMiddleware):
#     async def dispatch(self, request: Request, call_next):
#         app = request.app
#         if not hasattr(app.state, "postgres") or app.state.postgres is None:
#             await self._reinitialize_db(app)

#         try:
#             db_conn = app.state.postgres
#             async with db_conn.cursor() as cursor:
#                 await cursor.execute("SELECT 1")
#                 await cursor.fetchone()
#         except Exception as e:
#             logger.warning(f"Database connection is unavailable: {e}. Reconnecting...")
#             await self._reinitialize_db(app)

#         response = await call_next(request)
#         return response

#     async def _reinitialize_db(self, app: FastAPI):
#         db_connection = DatabaseConnection()
#         app.state.postgres = await db_connection.get_database_connection_fs()
#         logger.info("POSTGRES CLIENT HAS BEEN RE-INSTANTIATED 🛸")


def create_start_app_handler(app: FastAPI) -> Callable:
    async def start_app() -> None:
        es_connection = ElasticsearchConnection()
        app.state.elasticsearch = await es_connection.get_elasticsearch_client()
        logger.info("ELASTICSEARCH CLIENT HAS BEEN INSTANTIATED")

        # ElasticSearch Client with Compression
        app.state.elasticsearch_compressed = (
            await get_elasticsearch_client_with_compression()
        )
        logger.info("ELASTICSEARCH CLIENT WITH COMPRESSION HAS BEEN INSTANTIATED")

        # BD Elasticsearch client (separate credentials / cluster)
        es_url_bd = (os.getenv("ES_URL_BD") or "").strip()
        es_api_key_bd = (os.getenv("ES_API_KEY_BD") or "").strip()
        if es_url_bd and es_api_key_bd:
            app.state.elasticsearch_bd = AsyncElasticsearch(
                hosts=[es_url_bd],
                api_key=es_api_key_bd,
            )
            logger.info("BD ELASTICSEARCH CLIENT HAS BEEN INSTANTIATED")
        else:
            app.state.elasticsearch_bd = None
            logger.warning(
                "BD ELASTICSEARCH CLIENT NOT INITIALIZED (missing ES_URL_BD / ES_API_KEY_BD)"
            )

        # Manticore setup
        app.state.manticore = ManticoreConnection()
        await app.state.manticore.get_pool()
        logger.info("MANTICORE CLIENT INITIALIZED 🚀")

        # db_connection = DatabaseConnection()
        # app.state.postgres = await db_connection.get_database_connection_fs()
        # logger.info("POSTGRES CLIENT HAS BEEN INSTANTIATED 🚀")

        app.state.sqlalchemy_engine_ai = create_async_engine(
            URL.create(
                drivername="postgresql+asyncpg",
                username=os.getenv("DB_USERNAME"),
                password=os.getenv("DB_PASSWORD"),
                host=os.getenv("DB_HOST"),
                database=os.getenv("DB_DATABASE"),
                port="5432",
            ),
            pool_size=int(os.getenv("POOL_SIZE")),
            max_overflow=int(os.getenv("MAX_OVERFLOW")),
            pool_timeout=30,
            pool_recycle=1800,
            pool_pre_ping=True,
        )
        app.state.sqlalchemy_session_ai = async_scoped_session(
            sessionmaker(
                bind=app.state.sqlalchemy_engine_ai,
                class_=AsyncSession,
                expire_on_commit=False,
            ),
            scopefunc=current_task,
        )
        logger.info("SQLALCHEMY ENGINE AND SESSION HAVE BEEN INSTANTIATED 🚀")

    return start_app


def create_stop_app_handler(app: FastAPI) -> Callable:
    async def stop_app() -> None:
        ingestion_task = getattr(app.state, "ingestion_task", None)
        if ingestion_task:
            ingestion_task.cancel()
            try:
                await ingestion_task
            except asyncio.CancelledError:
                pass

        scheduled_check_task = getattr(app.state, "scheduled_check_task", None)
        if scheduled_check_task:
            scheduled_check_task.cancel()
            try:
                await scheduled_check_task
            except asyncio.CancelledError:
                pass

        if hasattr(app.state, "elasticsearch") and app.state.elasticsearch:
            await app.state.elasticsearch.close()
            logger.info("ELASTICSEARCH CLIENT HAS BEEN CLOSED 🚫")

        if hasattr(app.state, "elasticsearch_bd") and app.state.elasticsearch_bd:
            await app.state.elasticsearch_bd.close()
            logger.info("BD ELASTICSEARCH CLIENT HAS BEEN CLOSED 🚫")

        # In stop_app:
        if hasattr(app.state, "manticore") and app.state.manticore:
            await app.state.manticore.close()

        # if hasattr(app.state, "postgres") and app.state.postgres:
        #     await app.state.postgres.close()
        #     logger.info("POSTGRES CLIENT HAS BEEN CLOSED 🚫")

        if (
            hasattr(app.state, "sqlalchemy_engine_ai")
            and app.state.sqlalchemy_engine_ai
        ):
            await app.state.sqlalchemy_engine_ai.dispose()
            logger.info("SQLALCHEMY ENGINE HAS BEEN DISPOSED 🚫")

    return stop_app
