import traceback

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.models.schemas.company_data import (
    CompanyDataInput,
    CompanyDataOutput,
)
from app.routes.dependancies import get_es_client
from app.utils.icp.icp import get_additional_company_data

router = APIRouter()


@router.post("/get_company_data", response_model=CompanyDataOutput)
async def get_company_details(
    request: CompanyDataInput,
    es_client=Depends(get_es_client),
):
    try:
        company_data = await get_additional_company_data(
            request.company_urns, es_client, True
        )
        return JSONResponse(content=company_data)
    except Exception as e:
        print(e)
        traceback.print_exc()
        return JSONResponse(content={"error": str(e)}, status_code=500)
