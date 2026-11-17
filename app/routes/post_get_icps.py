import traceback
import asyncio

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.models.schemas.icp import (
    ICPInput,
    ICPOutput,
)
from app.routes.dependancies import get_es_client
from app.utils.icp.icp import get_icp
from app.utils.icp.icps_recommendation import suggest_icps, generate_profile_persona

router = APIRouter()


@router.post("/get_icps", response_model=ICPOutput)
async def get_icps(
    request: ICPInput,
    es_client=Depends(get_es_client),
):

    list_of_top_5_profiles = [c.list_of_top_5_profiles for c in request.clusters_list]
    summarized_cluster_info = [c.summary for c in request.clusters_list]
    user_public_identifier = request.user_public_identifier

    try:
        icp_task, persona_task = await asyncio.gather(
            get_icp(list_of_top_5_profiles, es_client),
            generate_profile_persona(user_public_identifier, es_client),
        )

        icps = icp_task
        profile_persona = persona_task

        existing_icps = {
            "existing_icps": [[d["title"] for d in inner_list] for inner_list in icps]
        }

        suggested_icps = await suggest_icps(
            profile_persona, summarized_cluster_info, existing_icps
        )

        return JSONResponse(content={"icps": icps, "suggested_icps": suggested_icps})

    except Exception as e:
        print(e)
        traceback.print_exc()
        return JSONResponse(content={"error": str(e)}, status_code=500)
