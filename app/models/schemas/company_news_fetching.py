from typing import List, Optional

from pydantic import BaseModel


from typing import List
from pydantic import BaseModel


class NewsInputCompany(BaseModel):
    company_universal_name: str
    company_urn: str


class NewsInput(BaseModel):
    companies: List[NewsInputCompany]
