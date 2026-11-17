from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.models.schemas.company_news_fetching import NewsInput
from app.routes.dependancies import get_es_client
from app.utils.news_fetching.company_news_fetching import (
    company_news_generator,
    fetch_company_names_bulk,
)

router = APIRouter()


@router.post("/stream_company_news")
async def stream_company_news(request: NewsInput, es_client=Depends(get_es_client)):
    company_info_map = await fetch_company_names_bulk(request.companies, es_client)
    generator = company_news_generator(request.companies, company_info_map, es_client)
    return StreamingResponse(generator, media_type="application/x-ndjson")
