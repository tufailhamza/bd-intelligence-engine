import asyncio
import sys

if sys.platform != "win32":
    import uvloop

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware

from app.api import router as api_router

from app.core.events import (
    ElasticsearchMiddleware,
    create_start_app_handler,
    create_stop_app_handler,
)

import uvicorn

app = FastAPI()

app.include_router(api_router)

app.add_middleware(GZipMiddleware, minimum_size=1000)

app.add_middleware(ElasticsearchMiddleware)

app.add_event_handler("startup", create_start_app_handler(app))
app.add_event_handler("shutdown", create_stop_app_handler(app))

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
