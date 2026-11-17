import traceback

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.models.schemas.network_clustering import (
    NetworkClusteringInput,
    NetworkClusteringOutput,
)
from app.routes.dependancies import get_es_client_with_compression
from app.utils.network_clustering.cluster_processing import (
    process_clusters,
    enrich_clusters_with_summary,
)
from app.utils.network_clustering.network_clustering import get_network_clustering

router = APIRouter()


@router.post("/network_clustering", response_model=NetworkClusteringOutput)
async def network_clustering(
    request: NetworkClusteringInput,
    es_client_compressed=Depends(get_es_client_with_compression),
):
    try:
        public_identifiers = request.public_identifiers

        if not public_identifiers:
            return JSONResponse(
                content={"error": "No public identifiers provided"}, status_code=400
            )

        cluster_details = await get_network_clustering(
            public_identifiers, es_client_compressed
        )

        summarized_clusters = await process_clusters(cluster_details)

        cluster_details = enrich_clusters_with_summary(
            cluster_details, summarized_clusters
        )

        clusters = {
            "network_clusters": cluster_details,
        }

        return JSONResponse(content=clusters)
    except Exception as e:
        print(e)
        traceback.print_exc()
        return JSONResponse(content={"error": str(e)}, status_code=500)
