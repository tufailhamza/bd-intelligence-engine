import re
import logging
from app.utils.news_fetching.prompts import RELEVANCE_PROMPT
from qutils.llm.asynchronous import invoke

logger = logging.getLogger(__name__)


async def call_llm_for_news_relevance(structured_input: str, target_company: str):
    """
    Call LLM to determine relevance of news to a target company.
    Parses the LLM response in:
    <id>True<source>XXX</source><news_type>XXX</news_type><news_subtype>XXX</news_subtype></id>

    Returns a list of dicts with:
    index, is_relevant, source, news_type, news_subtype.
    Gracefully handles errors or malformed entries.
    """
    prompt = RELEVANCE_PROMPT.format(
        structured_input=structured_input, target_company=target_company
    )

    try:
        response = await invoke(
            model="openai/gpt-4.1-mini",
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )

        # Strict regex: only matches fully valid entries
        pattern = (
            r"<(?P<id>\d+)>"  # capture ID
            r"(?P<relevant>True)"  # relevance must be True
            r"<source>(?P<source>.*?)</source>"  # source required
            r"<news_type>(?P<news_type>.*?)</news_type>"  # news_type required
            r"<news_subtype>(?P<news_subtype>.*?)</news_subtype>"  # news_subtype required
            r"</(?P=id)>"  # closing ID
        )

        matches = re.finditer(pattern, response, re.DOTALL)
        parsed = []

        for m in matches:
            data = m.groupdict()
            # Ensure no empty fields
            if all(
                data[k] and data[k].strip()
                for k in ["source", "news_type", "news_subtype"]
            ):
                parsed.append(
                    {
                        "index": int(data["id"]),
                        "is_relevant": True,
                        "source": data["source"].strip(),
                        "news_type": data["news_type"].strip(),
                        "news_subtype": data["news_subtype"].strip(),
                    }
                )
            else:
                logger.warning(f"Skipping malformed news entry: {m.group(0)[:100]}...")

        return parsed

    except Exception as e:
        logger.error(f"Error calling LLM for news relevance: {e}")
        return []
