from typing import List, Optional

from pydantic import BaseModel, Field


class KeywordTaglineInput(BaseModel):
    keywords: List[str] = Field(..., min_length=1, max_length=50)
    # Optional hint to guide the style (e.g. "professional", "playful", "B2B SaaS")
    style_hint: Optional[str] = None


class KeywordTaglineOutput(BaseModel):
    class KeywordInsight(BaseModel):
        keyword: str
        text: str

    insights: List[KeywordInsight]
