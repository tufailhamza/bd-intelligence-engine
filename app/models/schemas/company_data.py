from typing import List

from pydantic import BaseModel


class CompanyDataInput(BaseModel):
    company_urns: List[str]


class CompanyDataOutput(BaseModel):
    company_data: List[dict]
