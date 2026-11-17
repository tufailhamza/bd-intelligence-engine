import logging
import re

logger = logging.getLogger(__name__)


async def normalize_title(title):
    """Normalize a job title into a set of lowercase tokens without punctuation."""
    return set(re.sub(r"[^a-zA-Z ]", "", title.lower()).split())


async def get_most_distinct_titles(titles, top_n=5):
    """
    Group job titles by normalized token sets.
    Return up to top_n distinct titles (one per token set).
    """
    seen = set()
    buckets = {}
    for title in titles:
        tokens = frozenset(await normalize_title(title))
        if tokens not in seen:
            seen.add(tokens)
            buckets[tokens] = title
    distinct_titles = list(buckets.values())
    return distinct_titles[:top_n]


async def process_clusters(data, top_n=5):
    if not data:
        return None

    processed = []
    for cluster in data:
        combined = f"{cluster.get('cluster_name','')} - {cluster.get('cluster_description','')}"
        job_titles = cluster.get("job_titles", [])
        top_titles = await get_most_distinct_titles(job_titles, top_n=top_n)
        processed.append({"cluster": combined, "job_titles": top_titles})
    return processed


def enrich_clusters_with_summary(cluster_details, summarized_clusters):
    enriched_clusters = []

    for detail, summary in zip(cluster_details, summarized_clusters):
        detail_copy = dict(detail)
        company_size_summary = detail_copy.pop("company_size_title_stats", None)

        summary_copy = dict(summary)
        if company_size_summary:
            summary_copy["company_size_title_stats"] = company_size_summary

        detail_copy["summary"] = summary_copy
        enriched_clusters.append(detail_copy)

    return enriched_clusters
