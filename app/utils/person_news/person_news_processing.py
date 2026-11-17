import json
import logging
import uuid
import os
from datetime import datetime
from typing import List, Dict, Any, Optional

from qutils.llm.asynchronous import invoke
from app.core.manticore_client import ManticoreConnection
from elasticsearch import AsyncElasticsearch

import asyncio
import json
import logging

logger = logging.getLogger(__name__)

EXPIRATION_DAYS = 60
MANTICORE_TABLE = "bd_news"


async def _fetch_existing_news_ids_from_es(
    es_client_bd: Optional[AsyncElasticsearch],
    user_id: str,
    index_name: str = "bd_news",
) -> set:
    """
    Fetch existing news_ids from Elasticsearch bd_news index for a given user_id.
    Returns a set of news_id strings.
    """
    if not es_client_bd:
        logger.warning("ES client not available, skipping duplicate check")
        return set()

    try:
        body = {
            "query": {
                "bool": {"must": [{"term": {"linkedin_identifier.keyword": user_id}}]}
            },
            "_source": ["news_id"],
            "size": 1000,  # Limit to 1000 matches
        }

        res = await es_client_bd.search(index=index_name, body=body)
        hits = res.get("hits", {}).get("hits", []) or []

        existing_news_ids = set()
        for h in hits:
            src = h.get("_source") or {}
            news_id = src.get("news_id")
            if news_id:
                existing_news_ids.add(str(news_id))

        logger.info(
            f"Found {len(existing_news_ids)} existing news_ids in ES for user {user_id}"
        )
        return existing_news_ids
    except Exception as e:
        logger.error(f"Error fetching existing news_ids from ES: {e}")
        return set()


async def cache_person_news_manticore(
    user_id: str,
    posts: List[Dict[str, Any]],
    entity_urn: str,
    entity_id: str,
    manticore: ManticoreConnection,
    last_author_name: str = "",
    last_author_description: str = "",
    return_processed_posts: bool = False,
    es_client_bd: Optional[AsyncElasticsearch] = None,
) -> List[str] | Dict[str, Any]:
    """
    Cache LinkedIn person news into the unified bd_news table in Manticore.
    - Stores user_id into linkedin_identifier
    - Hardcodes entity_type='person_linkedin'
    - Hardcodes company_name="", link="", source="LinkedIn", news_sub_type=""
    """
    inserted_ids = []
    cache_date_str = int(datetime.utcnow().timestamp())

    # --- Step 1: Fetch all existing news_ids from Elasticsearch bd_news index ---
    existing_news_ids = await _fetch_existing_news_ids_from_es(
        es_client_bd=es_client_bd,
        user_id=user_id,
        index_name="bd_news",
    )

    # --- Step 2: Filter out posts that already exist ---
    filtered_posts_old = [
        post for post in posts if post.get("activityUrn") not in existing_news_ids
    ]

    pairs = set()
    filtered_posts = []
    for post in filtered_posts_old:
        if (post.get("linkedin_identifier"), post.get("activityUrn")) in pairs:
            continue
        pairs.add((post.get("linkedin_identifier"), post.get("activityUrn")))
        filtered_posts.append(post)

    logger.info(f"{len(filtered_posts)} posts remain after removing existing news_ids")

    if not filtered_posts:
        logger.info(f"No new posts to process for user_id={user_id}")
        if return_processed_posts:
            return {"inserted_ids": [], "processed_posts": []}
        return []

    # --- Step 3: Process LinkedIn posts ---
    processed_posts = await process_linkedin_posts(filtered_posts)

    if not processed_posts:
        logger.info(
            f"No news-worthy posts found after processing for user_id={user_id}"
        )
        if return_processed_posts:
            return {"inserted_ids": [], "processed_posts": []}
        return []

    # --- Step 4: Prepare batch insert ---
    values_list = []
    for post in processed_posts:
        news_id = post.get("activityUrn")
        if not news_id:
            logger.warning(
                f"Skipping post because activityUrn is missing: {post.get('newsTitle', '')}"
            )
            continue

        published_date_str = post.get("postDateISO") or cache_date_str
        if isinstance(published_date_str, str) and "T" in published_date_str:
            published_date_str = published_date_str.replace("T", " ")[:19]

        newsSubType = ""
        if post.get("newsType") == "company_news":
            newsSubType = "company_linkedin_repost"

        values_list.append(
            (
                news_id,
                user_id,
                entity_urn,
                entity_id,
                "person_linkedin",
                post.get("author"),
                post.get("author_description"),
                "",
                post.get("postURL", ""),
                "LinkedIn",
                published_date_str,
                post.get("postDateRelative"),
                post.get("newsTitle", ""),
                post.get("postText"),
                post.get("postType", "original"),
                post.get("newsType", ""),
                newsSubType,
                cache_date_str,
                EXPIRATION_DAYS,
                post.get("originalAuthorLinkedinIdentifier", ""),
                post.get("originalAuthorEntityId", ""),
                post.get("originalAuthorEntityUrn", ""),
                post.get("repostOriginalEntityType", ""),
                last_author_name or "",
                last_author_description or "",
                post.get("imageCount", 0),
                post.get("videoCount", 0),
            )
        )

    if not values_list:
        logger.info(f"No valid posts to insert for user_id={user_id}")
        if return_processed_posts:
            return {"inserted_ids": [], "processed_posts": processed_posts}
        return []

    try:
        placeholders = ",".join(["(" + ",".join(["%s"] * 27) + ")"] * len(values_list))
        flat_args = [item for sublist in values_list for item in sublist]

        query = f"""
            INSERT INTO {MANTICORE_TABLE} (
                news_id,
                linkedin_identifier,
                entity_urn,
                entity_id,
                entity_type,
                author,
                author_description,
                company_name,
                link,
                source,
                published_date,
                published_date_relative,
                news_title,
                content,
                post_type,
                news_type,
                news_sub_type,
                cache_date,
                expiration_days,
                original_author_linkedin_identifier,
                original_author_entity_id,
                original_author_entity_urn,
                repost_original_entity_type,
                last_author_name,
                last_author_description,
                imageCount,
                videoCount
            ) VALUES {placeholders}
        """

        await manticore.execute(query, flat_args)
        inserted_ids = [v[0] for v in values_list]
        logger.info(f"Inserted {len(inserted_ids)} new posts for user {user_id}")

    except Exception as e:
        logger.error(f"Error batch inserting posts into Manticore: {e}")

    if return_processed_posts:
        return {"inserted_ids": inserted_ids, "processed_posts": processed_posts}

    return inserted_ids


async def process_linkedin_posts(posts: List[Dict]) -> List[Dict]:
    """
    Process LinkedIn posts using LLM.
    - Batches posts in groups of 5
    - For each post, decide if it's news-worthy
    - Returns filtered/enriched posts
    """
    processed_posts = []
    semaphore = asyncio.Semaphore(4)  # max 4 batches concurrently

    # Assign numeric IDs to posts for reverse mapping
    post_map = {idx: post for idx, post in enumerate(posts)}

    # Prepare batches of 5 with minimal info for LLM
    batches = []
    for i in range(0, len(posts), 5):
        batch = []
        for idx, post in enumerate(posts[i : i + 5], start=i):
            # clean and truncate post text
            cleaned_text = post.get("postText", "").strip().replace("\n", " ")[:2000]
            batch.append(
                {
                    "id": idx,
                    "postText": cleaned_text,
                    "postType": "REPOST" if "repostedBy" in post else "ORIGINAL",
                }
            )
        batches.append(batch)

    async def process_single_batch(batch):
        async with semaphore:
            prompt = f"""
            You are analyzing LinkedIn posts.  
            For each post, decide if it contains meaningful *news* related to the following types only: 
            **job_change, achievements, engagement, publication, social_impact, company_news**.  

            Rules:
            - Input: list of posts (text + type only).
            - Consider posts authored by a person OR a company.
            - If the post relates to company-level updates (e.g., IPOs, funding, hiring, launches, partnerships), classify as **company_news**.
            - If the post relates to an individual’s updates, classify using one of:
                - **job_change**: new role, promotion, or leaving a role
                - **achievements**: awards, recognitions, milestones, personal accomplishments, invitations for speaking, competitions, honors
                - **engagement**: participation in events, volunteering, community involvement
                - **publication**: articles, papers, or content authored by the person
                - **social_impact**: initiatives, charity work, or contributions affecting others
                - **personal_reflection**: notable experiences, stories, or milestones the person shares that are meaningful but do not fit the above categories
            - If a post does not clearly relate to company or individual updates, mark it as **not_news**.
            - Use context clues like mentions of “first time,” “invitation,” “honor,” or “congratulations” to identify personal achievements or reflections.


            Output:
            - JSON array, same order as input, each element must be:
            {{
                "id": id,
                "is_news": true/false,
                "news_title": "short title if is_news",
                "news_type": "category if is_news (job_change, achievements, engagement, publication, social_impact, company_news)"
            }}

            If is_news=false, only return: {{"id": id, "is_news": false}}.

            Now analyze these posts:

            {json.dumps(batch, indent=2)}

            First give small reasoning and analysis of the post and then give the final JSON array as specified above.
            """

            try:
                response = await invoke(
                    model="openai/gpt-4.1-mini",
                    temperature=0,
                    fallbacks=[
                        "openai/gpt-4.1",
                        "openai/gpt-4o-mini",
                        "openai/gpt-4o",
                        "google/gemini-2.5-flash",
                        "google/gemini-2.5-pro",
                        "anthropic/claude-sonnet-4-5-20250929",
                    ],
                    messages=[{"role": "user", "content": prompt}],
                )
                parsed = []
                try:
                    parsed = json.loads(response)
                except Exception:
                    response_str = str(response)
                    json_start = response_str.find("[")
                    json_end = response_str.rfind("]") + 1
                    if json_start != -1 and json_end != -1:
                        parsed = json.loads(response_str[json_start:json_end])

                # Attach LLM output back to original posts
                for item in parsed:
                    if item.get("is_news"):
                        orig_post = post_map[item["id"]]
                        processed_posts.append(
                            {
                                "activityUrn": orig_post.get("activityUrn"),
                                "postText": orig_post.get(
                                    "postText"
                                ),  # full original text
                                "postType": (
                                    "REPOST"
                                    if "repostedBy" in orig_post
                                    else "ORIGINAL"
                                ),
                                "newsTitle": item.get("news_title", ""),
                                "newsType": item.get("news_type", ""),
                                "postDateISO": orig_post.get("postDateISO"),
                                "postDateRelative": orig_post.get("postDateRelative"),
                                "author": orig_post.get("accountName")
                                or orig_post.get("name", ""),
                                "author_description": orig_post.get("description", ""),
                                "originalAuthorEntityId": orig_post.get(
                                    "originalAuthorEntityId", ""
                                ),
                                "originalAuthorEntityUrn": orig_post.get(
                                    "originalAuthorEntityUrn", ""
                                ),
                                "repostOriginalEntityType": orig_post.get(
                                    "repostOriginalEntityType", ""
                                ),
                                "originalAuthorLinkedinIdentifier": orig_post.get(
                                    "originalAuthorLinkedinIdentifier", ""
                                ),
                                "postURL": orig_post.get("postURL", ""),
                                "imageCount": orig_post.get("imageCount", 0),
                                "videoCount": orig_post.get("videoCount", 0),
                            }
                        )

            except Exception as e:
                logger.error(f"Error while processing batch via LLM: {e}")

    # Run all batches concurrently (max 4 at a time)
    await asyncio.gather(*(process_single_batch(batch) for batch in batches))

    return processed_posts
