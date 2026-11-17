import logging
import os

from elasticsearch import AsyncElasticsearch

logger = logging.getLogger(__name__)


async def get_unique_companies_from_matching_profiles(
    matches: list,
    es_client: AsyncElasticsearch,
    es_client_compressed: AsyncElasticsearch,
):
    try:

        if not await es_client_compressed.ping():
            logger.error("Custom Elasticsearch cluster unreachable.")
            await es_client_compressed.close()
            return [], {}

        ids = [m["id"] for m in matches]
        id_to_hits = {m["id"]: m["experienceHits"] for m in matches}

        response = await es_client_compressed.search(
            index=os.getenv("ES_PROFILES_INDEX"),
            query={"terms": {"_id": ids}},
            size=len(ids),
            _source=["experience"],
        )

        await es_client_compressed.close()

        company_to_profiles = {}
        company_details_map = {}

        for hit in response["hits"]["hits"]:
            _id = hit["_id"]
            profile = hit["_source"]
            experiences = profile.get("experience", [])
            experience_hits = id_to_hits.get(_id, [])

            for index in experience_hits:
                if index < len(experiences):
                    exp = experiences[index]
                    company_urn = exp.get("company_urn")

                    if not company_urn:
                        continue

                    company_to_profiles.setdefault(company_urn, []).append(_id)

                    if company_urn not in company_details_map:
                        industry = exp.get("company_industry", [])
                        if industry and isinstance(industry, str):
                            industry = [industry]

                        company_details_map[company_urn] = {
                            "company": exp.get("company"),
                            "company_urn": company_urn,
                            "company_universal_name": exp.get("company_universal_name"),
                            "company_size": exp.get("company_size"),
                            "company_type": exp.get("company_type"),
                            "company_industry": industry,
                        }

        company_profile_list = [
            {"company_id": cid, "profile_ids": pids}
            for cid, pids in company_to_profiles.items()
        ]

        return company_profile_list, company_details_map

    except Exception as e:
        logger.exception("Error in get_unique_companies_from_matching_profiles")
        return [], {}
