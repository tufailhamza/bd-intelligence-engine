from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio.session import AsyncSession


def get_es_client(request: Request):
    return request.app.state.elasticsearch


def get_es_client_with_compression(request: Request):
    return request.app.state.elasticsearch_compressed


def get_es_client_bd(request: Request):
    return request.app.state.elasticsearch_bd


def get_db_client(request: Request):
    return request.app.state.postgres


def get_manticore_client(request: Request):
    return request.app.state.manticore


@asynccontextmanager
async def get_sqlalchemy_session_ai() -> AsyncIterator[AsyncSession]:
    from main import app

    session = app.state.sqlalchemy_session_ai()
    try:
        yield session
    finally:
        await session.close()
