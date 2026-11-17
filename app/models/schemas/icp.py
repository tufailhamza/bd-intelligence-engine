from typing import List

from pydantic import BaseModel


class ClusterItem(BaseModel):
    list_of_top_5_profiles: List[dict]
    summary: dict


class ICPInput(BaseModel):
    user_public_identifier: str
    clusters_list: List[ClusterItem]


class ICPOutput(BaseModel):
    icps: List[List[dict]]
    suggested_icps: List[dict]
