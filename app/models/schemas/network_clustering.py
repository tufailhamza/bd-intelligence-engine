from typing import List, Any

from pydantic import BaseModel


class NetworkClusteringInput(BaseModel):
    public_identifiers: List[str]


class Summary(BaseModel):
    cluster: str
    job_titles: List[str]


class Cluster(BaseModel):
    cluster_name: str
    job_titles: List[str]
    industry: List[str]
    cluster_description: str
    profiles: List[Any]
    top_5_profiles: List[Any]
    summary: Summary


class NetworkClusteringOutput(BaseModel):
    network_clusters: List[Cluster]
