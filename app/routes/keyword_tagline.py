import json
import logging
import re
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from qutils.llm.asynchronous import invoke

from app.models.schemas.keyword_tagline import KeywordTaglineInput, KeywordTaglineOutput

logger = logging.getLogger(__name__)
router = APIRouter()


def _safe_json_loads(text: str) -> Any:
    if not text:
        raise ValueError("Empty response")
    t = str(text).strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", t).replace("```", "").strip()
    obj = re.search(r"\{[\s\S]*\}", t)
    return json.loads(obj.group(0) if obj else t)


@router.post("/keyword_tagline", response_model=KeywordTaglineOutput)
async def keyword_tagline(request: KeywordTaglineInput):
    """
    Accepts multiple keywords and returns insight strings.
    """
    try:
        keywords = [
            k.strip() for k in request.keywords if isinstance(k, str) and k.strip()
        ]
        style = (request.style_hint or "").strip()

        if not keywords:
            return JSONResponse(content={"insights": []})

        prompt = f"""
You generate short, natural-sounding insight sentences based on a list of keywords.
Guide the user on the domains of the coonection there connections are instrested

KEYWORDS (JSON array of strings):
{json.dumps(keywords, ensure_ascii=False)}

STYLE_HINT:
{style or "professional"}

Return ONLY valid JSON (no markdown, no extra text):
{{"insights":[{{"keyword":"<keyword>","text":"<one short sentence>"}}]}}

EXAMPLE:
    Keyword:FinTech
    Response:
    Text: Contacts in your network are interested in FinTech
Rules:
- Keep it concise (max 14 words per insight)
- The text should be dynamic and specific to the keyword
- Do not include numbers or counts
- Do not invent new keywords; keyword must match exactly
""".strip()

        resp = await invoke(
            model="openai/gpt-4o",
            temperature=0.2,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
        )

        parsed = _safe_json_loads(str(resp))
        insights = []
        if isinstance(parsed, dict) and isinstance(parsed.get("insights"), list):
            for item in parsed["insights"]:
                if not isinstance(item, dict):
                    continue
                kw = item.get("keyword")
                txt = item.get("text")
                if (
                    isinstance(kw, str)
                    and isinstance(txt, str)
                    and kw.strip()
                    and txt.strip()
                ):
                    insights.append({"keyword": kw.strip(), "text": txt.strip()})

        # Ensure we return exactly one insight per input keyword (stable + predictable)
        by_kw = {x["keyword"]: x["text"] for x in insights if isinstance(x, dict)}
        final_insights = []
        for kw in keywords:
            text = by_kw.get(kw)
            if not text:
                text = f"People in your domain are from {kw}"
            final_insights.append({"keyword": kw, "text": text})

        return JSONResponse(content={"insights": final_insights})
    except Exception as e:
        logger.error("Unhandled exception in /keyword_tagline: %s", str(e))
        return JSONResponse(content={"error": "Internal Server Error"}, status_code=500)
