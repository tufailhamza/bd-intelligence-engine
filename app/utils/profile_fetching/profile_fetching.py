import asyncio
import json
import logging
import math
import os
import time
import traceback
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Dict, Optional
from uuid import UUID

import httpx
import numpy as np
from aiohttp import ClientSession, ClientTimeout
from elasticsearch import AsyncElasticsearch

from app.database.utilities import (
    get_cached_icp_profiles,
    update_cached_profiles_only,
    upsert_cached_icp_profiles,
)
from app.utils.icp.icp import get_additional_company_data
from app.utils.profile_fetching.companies_data_fetch import (
    get_unique_companies_from_matching_profiles,
)
from app.utils.profile_fetching.prompts import QUERY_WRITER
from app.utils.util_functions import (
    MAP_TIMELINE,
    clean_results,
    extract_generic,
    get_company_ownership_data,
    get_expert_response,
    get_prompt_expert,
    reverse_output_to_input,
    write_logs,
)
from qutils.llm.utilities import asynchronous_llm

ICP_PROFILES_TABLE = "icp_profiles_cache"
MAX_TIMEOUT = 600

logger = logging.getLogger(__name__)


async def call_l0_filter_extraction(query: str, es_client: AsyncElasticsearch):
    url = os.getenv("L0_FILTER_EXTRACTION_URL")

    if not url:
        logger.error("L0 filter extraction URL not found in environment variables.")
        return {}

    try:
        start = time.perf_counter()
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url=url,
                json={"query": query, "isDemographicsBlocked": True},
                timeout=MAX_TIMEOUT,
            )
        end = time.perf_counter()
        logging.info(f"L0 filter Extraction took {end - start} seconds")
        if response.status_code != 200:
            logger.error(
                f"L0 filter API failed with status {response.status_code}: {response.text}"
            )
            return {}

        return response.json()

    except Exception as e:
        logger.error("Exception during L0 filter extraction")
        return {}


async def call_comp_gen_endpoint(query: str, es_client: AsyncElasticsearch):

    try:
        async with httpx.AsyncClient() as client:
            start = time.perf_counter()
            response = await client.post(
                url=os.getenv("SHORTEN_PROMPT_URL", ""),
                json={"prompt": query, "agent": "dual"},
                timeout=MAX_TIMEOUT,
            )
            end = time.perf_counter()
            logging.info(f"Shorten Prompt URL took {end - start} seconds")
            shorten_prompt_response = response.json()
    except Exception as err:
        logger.error(f"Error in Shorten Prompt URL {err}")
        return [], [], "CURRENT"

    if not shorten_prompt_response:
        return [], [], "CURRENT"

    if not shorten_prompt_response["current"]:
        shorten_prompt_response["current"] = ""

    if not shorten_prompt_response["past"]:
        shorten_prompt_response["past"] = ""

    event = shorten_prompt_response["timeline"]
    if event == "OR":
        current_prompt = (
            shorten_prompt_response["current"] + shorten_prompt_response["past"]
        )
        past_prompt = ""
    else:
        current_prompt = shorten_prompt_response["current"]
        past_prompt = shorten_prompt_response["past"]

    payload = {
        "prompt": "",
        "current_prompt": current_prompt,
        "past_prompt": past_prompt,
        "userquery": "",
        "context": [],
        "description": False,
        "agent": "dual",
    }

    current_comps = []
    past_comps = []

    try:
        start = time.perf_counter()
        async with ClientSession() as session:
            async with session.post(
                os.getenv("COMP_GEN_URL", ""),
                json=payload,
                timeout=ClientTimeout(total=MAX_TIMEOUT),
            ) as response:
                if response.status == 200:
                    async for chunk in response.content:
                        if chunk:
                            chunk = chunk.strip()
                            chunk = chunk.decode()
                            if chunk:
                                chunk = chunk.replace("data: ", "")
                                try:
                                    chunk = json.loads(chunk)
                                except Exception as decode_err:
                                    logger.warning(
                                        f"Skipping bad chunk due to decode error: {decode_err}"
                                    )
                                    continue
                                if chunk["type"] == "CURRENT":
                                    current_comps.append(
                                        {
                                            "id": chunk["es_data"]["es_id"],
                                            "name": chunk["es_data"]["name"],
                                            "industry": chunk["es_data"]["industry"],
                                            "universalName": chunk["es_data"][
                                                "universalName"
                                            ],
                                            "urn": chunk["es_data"]["urn"],
                                            "logo": "",
                                        }
                                    )
                                else:
                                    past_comps.append(
                                        {
                                            "id": chunk["es_data"]["es_id"],
                                            "name": chunk["es_data"]["name"],
                                            "industry": chunk["es_data"]["industry"],
                                            "universalName": chunk["es_data"][
                                                "universalName"
                                            ],
                                            "urn": chunk["es_data"]["urn"],
                                            "logo": "",
                                        }
                                    )
        end = time.perf_counter()
        logging.info(f"Company Gen URL took {end - start} seconds")
        return current_comps, past_comps, event
    except Exception as e:
        logger.error(f"Error in Comp Gen URL service {e}")
        return [], [], "CURRENT"


async def process_approaches(approaches: list):

    MAP_TIMELINE = {
        "CURRENT": "Current",
        "PAST": "Past",
        "OR": "Current or Past",
        "AND": "Current and Past",
        "CURRENT OR PAST": "Current or Past",
        "CURRENT AND PAST": "Current and Past",
    }

    MAP_MANAGEMENT_LEVELS = {
        "Senior (Individual Contributor)": "Senior (Individual contributor)",
        "Mid (Individual Contributor)": "Mid (Individual contributor)",
    }

    processed_approaches = []
    comp_gen_tasks = []
    comp_gen_indexes = []
    comp_gen_timelines = []

    for idx, approach in enumerate(approaches):
        filters = {}
        for filter, filter_values in approach.items():
            if filter == "title" and filter_values["filter"]:
                filters["job_titles_timeline"] = MAP_TIMELINE[filter_values["event"]]
                job_titles = {"current": {}, "previous": {}}

                for title, title_attributes in filter_values["filter"].items():
                    if (
                        title_attributes["type"] == "CURRENT"
                        or title_attributes["type"] == "OR"
                    ):
                        job_titles["current"][title] = (
                            title_attributes["min"],
                            title_attributes["max"],
                        )
                    elif title_attributes["type"] == "PAST":
                        job_titles["previous"][title] = (
                            title_attributes["min"],
                            title_attributes["max"],
                        )
                    elif (
                        title_attributes["type"] == "AND"
                        or title_attributes["type"] == "BOTH"
                    ):
                        job_titles["previous"][title] = (
                            title_attributes["min"],
                            title_attributes["max"],
                        )
                        job_titles["current"][title] = (
                            title_attributes["min"],
                            title_attributes["max"],
                        )
                    else:
                        raise Exception(
                            f"Invalid job title timeline = {title_attributes['type']}"
                        )

                filters["job_titles"] = job_titles

            elif filter == "skill" and filter_values["filter"]:
                filters["skills_timeline"] = MAP_TIMELINE[filter_values["event"]]

                skills = {"current": [], "previous": []}

                for skill, skill_attributes in filter_values["filter"].items():
                    skill = (skill, skill_attributes.get("logic", "OR"))

                    if (
                        skill_attributes["type"] == "CURRENT"
                        or skill_attributes["type"] == "OR"
                    ):
                        skills["current"].append(skill)

                    elif skill_attributes["type"] == "PAST":
                        skills["previous"].append(skill)

                    elif (
                        skill_attributes["type"] == "AND"
                        or skill_attributes["type"] == "BOTH"
                    ):
                        skills["previous"].append(skill)
                        skills["current"].append(skill)

                    else:
                        raise Exception(
                            f"Invalid skills timeline = {skill_attributes['type']}"
                        )

                filters["skills"] = skills

            elif filter == "management_level" and filter_values["filter"]:
                filters["management_levels_timeline"] = MAP_TIMELINE[
                    filter_values["event"]
                ]

                management_levels = {"current": [], "previous": []}

                for management_level, management_level_attributes in filter_values[
                    "filter"
                ].items():
                    management_level = MAP_MANAGEMENT_LEVELS.get(
                        management_level, management_level
                    )

                    if (
                        management_level_attributes["type"] == "CURRENT"
                        or management_level_attributes["type"] == "OR"
                    ):
                        management_levels["current"].append(management_level)

                    elif management_level_attributes["type"] == "PAST":
                        management_levels["previous"].append(management_level)

                    elif (
                        management_level_attributes["type"] == "AND"
                        or management_level_attributes["type"] == "BOTH"
                    ):
                        management_levels["previous"].append(management_level)
                        management_levels["current"].append(management_level)

                    else:
                        raise Exception(
                            f"Invalid magamenet level timeline = {management_level_attributes['type']}"
                        )

                filters["management_levels"] = management_levels

            elif filter == "location" and filter_values["filter"]:
                filters["locations_timeline"] = MAP_TIMELINE[filter_values["event"]]

                locations = {"current": [], "previous": []}

                for location, location_attributes in filter_values["filter"].items():
                    if (
                        location_attributes["type"] == "CURRENT"
                        or location_attributes["type"] == "OR"
                    ):
                        locations["current"].append(location)

                    elif location_attributes["type"] == "PAST":
                        locations["previous"].append(location)

                    elif (
                        location_attributes["type"] == "AND"
                        or location_attributes["type"] == "BOTH"
                    ):
                        locations["previous"].append(location)
                        locations["current"].append(location)

                    else:
                        raise Exception(
                            f"Invalid location timeline = {location_attributes['type']}"
                        )

                filters["locations"] = locations

            elif filter == "industry" and filter_values["filter"]:
                filters["industries_timeline"] = MAP_TIMELINE[filter_values["event"]]

                industries = {"current": [], "previous": []}

                for industry, industry_attributes in filter_values["filter"].items():
                    if (
                        industry_attributes["type"] == "CURRENT"
                        or industry_attributes["type"] == "OR"
                    ):
                        industries["current"].append(industry)

                    elif industry_attributes["type"] == "PAST":
                        industries["previous"].append(industry)

                    elif (
                        industry_attributes["type"] == "AND"
                        or industry_attributes["type"] == "BOTH"
                    ):
                        industries["previous"].append(industry)
                        industries["current"].append(industry)

                    else:
                        raise Exception(
                            f"Invalid industry timeline = {industry_attributes['type']}"
                        )

                filters["industries"] = industries

            elif filter == "role_tenure" and filter_values:
                min_role_tenure, max_role_tenure = (
                    filter_values.get("min", None),
                    filter_values.get("max", None),
                )
                if min_role_tenure:
                    filters["role_tenure_min"] = min_role_tenure

                if max_role_tenure:
                    filters["role_tenure_max"] = max_role_tenure

                filters["is_role_tenure_selected"] = True

            elif filter == "experience" and filter_values:
                min_experience, max_experience = (
                    filter_values.get("min", None),
                    filter_values.get("max", None),
                )
                if min_experience:
                    filters["experience_min"] = min_experience

                if max_experience:
                    filters["experience_max"] = max_experience

                filters["is_experience_selected"] = True

            elif filter == "company_tenure" and filter_values:
                min_company_tenure, max_company_tenure = (
                    filter_values.get("min", None),
                    filter_values.get("max", None),
                )
                if min_company_tenure:
                    filters["company_tenure_min"] = min_company_tenure

                if max_company_tenure:
                    filters["company_tenure_max"] = max_company_tenure

                filters["is_company_tenure_selected"] = True
            elif filter == "education":
                education_array = filter_values
                filters["education"] = education_array

            elif filter == "school":
                filters["university"] = filter_values

        processed_approaches.append(filters)
    comp_gen_res = await asyncio.gather(*comp_gen_tasks)

    for idx, (current_companies, previous_companies) in enumerate(comp_gen_res):
        approach_index = comp_gen_indexes[idx]

        current_companies = [
            {
                "id": company["es_data"]["es_id"],
                "name": company["es_data"]["name"],
                "industry": company["es_data"]["industry"],
                "universalName": company["es_data"]["universalName"],
                "urn": company["es_data"]["urn"],
                "logo": "",
            }
            for company in current_companies
        ]

        previous_companies = [
            {
                "id": company["es_data"]["es_id"],
                "name": company["es_data"]["name"],
                "industry": company["es_data"]["industry"],
                "universalName": company["es_data"]["universalName"],
                "urn": company["es_data"]["urn"],
                "logo": "",
            }
            for company in previous_companies
        ]

        companies_object = {
            "companies": {"current": current_companies, "previous": previous_companies},
            "companies_timeline": comp_gen_timelines[idx],
        }

        processed_approaches[approach_index].update(companies_object)

    return processed_approaches


def create_search_payload(
    conversation_id=None,
    past_company_conversation_id=None,
    job_titles=None,
    job_titles_timeline="Current",
    management_levels=None,
    management_levels_timeline="Current",
    companies=None,
    companies_timeline="Current",
    industries=None,
    industries_timeline="Current",
    locations=None,
    locations_timeline="Current",
    skills=None,
    skills_timeline="Current or Past",
    experience_min=1,
    experience_max=100,
    is_experience_selected=False,
    role_tenure_min=1,
    role_tenure_max=100,
    is_role_tenure_selected=False,
    company_tenure_min=1,
    company_tenure_max=100,
    is_company_tenure_selected=False,
    university=None,
    education=None,
):
    VALID_TIMELINES = {"Current", "Past", "Current or Past", "Current and Past"}
    VALID_MANAGEMENT_LEVELS = {
        "Partners",
        "Founder/Co-Founder",
        "Board of Directors",
        "C Suite",
        "President",
        "Executive and Sr. VP",
        "VP",
        "Director",
        "Manager",
        "Senior (Individual contributor)",
        "Mid (Individual contributor)",
        "Junior",
        "Head",
    }

    base_payload = {
        "jobTitles": {
            "innerLogic": "OR",
            "current": {"isStrict": False, "isRemoved": [], "isSelected": []},
            "previous": {"isStrict": False, "isRemoved": [], "isSelected": []},
            "either": {"isStrict": False, "isRemoved": [], "isSelected": []},
        },
        "company": {
            "innerLogic": "OR",
            "current": {"isStrict": False, "isRemoved": [], "isSelected": []},
            "previous": {"isStrict": False, "isRemoved": [], "isSelected": []},
            "either": {"isStrict": False, "isRemoved": [], "isSelected": []},
        },
        "locations": {
            "innerLogic": "OR",
            "current": {"isStrict": False, "isRemoved": [], "isSelected": []},
            "previous": {"isStrict": False, "isRemoved": [], "isSelected": []},
        },
        "managementTitles": {
            "innerLogic": "OR",
            "current": {"isStrict": False, "isRemoved": [], "isSelected": []},
            "previous": {"isStrict": False, "isRemoved": [], "isSelected": []},
            "either": {"isStrict": False, "isRemoved": [], "isSelected": []},
        },
        "education": {
            "innerLogic": "OR",
            "current": {"isStrict": False, "isRemoved": [], "isSelected": []},
            "previous": {"isStrict": False, "isRemoved": [], "isSelected": []},
        },
        "companyOwnershipTitles": {
            "innerLogic": "OR",
            "current": {"isStrict": False, "isRemoved": [], "isSelected": []},
            "previous": {"isStrict": False, "isRemoved": [], "isSelected": []},
        },
        "university": {
            "innerLogic": "OR",
            "current": {"isStrict": False, "isRemoved": [], "isSelected": []},
            "previous": {"isStrict": False, "isRemoved": [], "isSelected": []},
        },
        "industries": {
            "innerLogic": "OR",
            "current": {"isStrict": False, "isRemoved": [], "isSelected": []},
            "previous": {"isStrict": False, "isRemoved": [], "isSelected": []},
        },
        "skills": {
            "innerLogic": "OR",
            "current": {"isStrict": False, "isRemoved": [], "isSelected": []},
            "previous": {"isStrict": False, "isRemoved": [], "isSelected": []},
            "either": {"isStrict": False, "isRemoved": [], "isSelected": []},
        },
        "product": {
            "innerLogic": "OR",
            "current": {"isStrict": False, "isRemoved": [], "isSelected": []},
            "previous": {"isStrict": False, "isRemoved": [], "isSelected": []},
        },
        "profileNames": {"isSelected": [], "isRemoved": []},
        "experience": {"total": {"min": 1, "max": 100}},
        "roleTenure": {"total": {"min": 1, "max": 100}},
        "companyTenure": {"total": {"min": 1, "max": 100}},
        "demographic": {
            "innerLogic": "OR",
            "current": {"isStrict": False, "isRemoved": [], "isSelected": []},
            "previous": {"isStrict": False, "isRemoved": [], "isSelected": []},
        },
        "isCompanyTenureSelected": False,
        "isRoleTenureSelected": False,
        "isExperienceSelected": False,
        "isCompanyOwnershipSelected": False,
    }
    if conversation_id is not None:
        base_payload["conversationId"] = conversation_id
    if past_company_conversation_id is not None:
        base_payload["pastCompanyConversationId"] = past_company_conversation_id
    base_payload["experience"]["total"]["min"] = experience_min
    base_payload["experience"]["total"]["max"] = experience_max
    base_payload["isExperienceSelected"] = is_experience_selected
    base_payload["roleTenure"]["total"]["min"] = role_tenure_min
    base_payload["roleTenure"]["total"]["max"] = role_tenure_max
    base_payload["isRoleTenureSelected"] = is_role_tenure_selected
    base_payload["companyTenure"]["total"]["min"] = company_tenure_min
    base_payload["companyTenure"]["total"]["max"] = company_tenure_max
    base_payload["isCompanyTenureSelected"] = is_company_tenure_selected

    def place_items_in_timeline(
        items_current,
        items_previous,
        timeline,
        add_to_current,
        add_to_previous,
        add_to_either,
    ):
        if timeline not in VALID_TIMELINES:
            raise ValueError(f"Invalid timeline: {timeline}")
        items_current = items_current or []
        items_previous = items_previous or []
        if timeline == "Current":
            for ic in items_current:
                add_to_current(ic)
        elif timeline == "Past":
            for ip in items_previous:
                add_to_previous(ip)
        elif timeline == "Current or Past":
            union = []
            for ic in items_current:
                if ic not in union:
                    union.append(ic)
            for ip in items_previous:
                if ip not in union:
                    union.append(ip)
            for item in union:
                add_to_either(item)
            # for ic in items_current:
            #     add_to_either(ic)
            # for ip in items_previous:
            #     add_to_either(ip)
        elif timeline == "Current and Past":
            # union = list(items_current) + list(items_previous)
            # for item in union:
            #     add_to_current(item)
            #     add_to_previous(item)
            for ic in items_current:
                add_to_current(ic)
            for ip in items_previous:
                add_to_previous(ip)

    def add_job_title_to_current(data):
        name, (mn, mx) = data
        base_payload["jobTitles"]["current"]["isSelected"].append(
            {"name": name, "min": mn, "max": mx}
        )

    def add_job_title_to_previous(data):
        name, (mn, mx) = data
        base_payload["jobTitles"]["previous"]["isSelected"].append(
            {"name": name, "min": mn, "max": mx}
        )

    def add_job_title_to_either(data):
        name, (mn, mx) = data
        base_payload["jobTitles"]["either"]["isSelected"].append(
            {"name": name, "min": mn, "max": mx}
        )

    if job_titles:
        current_dict = job_titles.get("current", {})
        previous_dict = job_titles.get("previous", {})

        if not current_dict:
            current_dict = {}
        if not previous_dict:
            previous_dict = {}

        place_items_in_timeline(
            items_current=list(current_dict.items()),
            items_previous=list(previous_dict.items()),
            timeline=job_titles_timeline,
            add_to_current=add_job_title_to_current,
            add_to_previous=add_job_title_to_previous,
            add_to_either=add_job_title_to_either,
        )

    def add_mgmt_current(ml):
        if ml == "C-Suite and President":
            ml = "CSuite and President"
        if ml not in VALID_MANAGEMENT_LEVELS:
            raise ValueError(
                f"Invalid management level '{ml}'. Must be in {VALID_MANAGEMENT_LEVELS}."
            )
        base_payload["managementTitles"]["current"]["isSelected"].append({"name": ml})

    def add_mgmt_previous(ml):
        if ml == "C-Suite and President":
            ml = "CSuite and President"
        if ml not in VALID_MANAGEMENT_LEVELS:
            raise ValueError(
                f"Invalid management level '{ml}'. Must be in {VALID_MANAGEMENT_LEVELS}."
            )
        base_payload["managementTitles"]["previous"]["isSelected"].append({"name": ml})

    def add_mgmt_either(ml):
        if ml == "C-Suite and President":
            ml = "CSuite and President"
        if ml not in VALID_MANAGEMENT_LEVELS:
            raise ValueError(
                f"Invalid management level '{ml}'. Must be in {VALID_MANAGEMENT_LEVELS}."
            )
        base_payload["managementTitles"]["either"]["isSelected"].append({"name": ml})

    if management_levels:
        items_current = management_levels.get("current", [])
        items_previous = management_levels.get("previous", [])
        place_items_in_timeline(
            items_current,
            items_previous,
            management_levels_timeline,
            add_to_current=add_mgmt_current,
            add_to_previous=add_mgmt_previous,
            add_to_either=add_mgmt_either,
        )

    def add_company_current(cdict):
        if isinstance(cdict, list):
            for c in cdict:
                cpy = dict(c)
                cpy["logo"] = ""
                base_payload["company"]["current"]["isSelected"].append(cpy)
        else:
            cpy = dict(cdict)
            cpy["logo"] = ""
            base_payload["company"]["current"]["isSelected"].append(cpy)

    def add_company_previous(cdict):
        if isinstance(cdict, list):
            for c in cdict:
                cpy = dict(c)
                cpy["logo"] = ""
                base_payload["company"]["previous"]["isSelected"].append(cpy)
        else:
            cpy = dict(cdict)
            cpy["logo"] = ""
            base_payload["company"]["previous"]["isSelected"].append(cpy)

    def add_company_either(cdict):
        if isinstance(cdict, list):
            for c in cdict:
                cpy = dict(c)
                cpy["logo"] = ""
                base_payload["company"]["current"]["isSelected"].append(cpy)
        else:
            cpy = dict(cdict)
            cpy["logo"] = ""
            base_payload["company"]["current"]["isSelected"].append(cpy)

    if companies:
        items_current = companies.get("current", [])
        items_previous = companies.get("previous", [])

        place_items_in_timeline(
            items_current,
            items_previous,
            companies_timeline,
            add_to_current=add_company_current,
            add_to_previous=add_company_previous,
            add_to_either=add_company_either,
        )

    def add_industry_current(ind):
        base_payload["industries"]["current"]["isSelected"].append({"name": ind})

    def add_industry_previous(ind):
        base_payload["industries"]["previous"]["isSelected"].append({"name": ind})

    def add_industry_either(ind):
        base_payload["industries"]["either"]["isSelected"].append({"name": ind})

    if industries:
        if "either" not in base_payload["industries"]:
            base_payload["industries"]["either"] = {
                "isStrict": False,
                "isRemoved": [],
                "isSelected": [],
            }
        items_current = industries.get("current", [])
        items_previous = industries.get("previous", [])
        place_items_in_timeline(
            items_current,
            items_previous,
            industries_timeline,
            add_to_current=add_industry_current,
            add_to_previous=add_industry_previous,
            add_to_either=add_industry_either,
        )

    def add_location_current(loc_str):
        base_payload["locations"]["current"]["isSelected"].append({"name": loc_str})

    def add_location_previous(loc_str):
        base_payload["locations"]["previous"]["isSelected"].append({"name": loc_str})

    def add_location_either(loc_str):
        if "either" not in base_payload["locations"]:
            base_payload["locations"]["either"] = {
                "isStrict": False,
                "isRemoved": [],
                "isSelected": [],
            }
        base_payload["locations"]["either"]["isSelected"].append({"name": loc_str})

    if locations:
        items_current = locations.get("current", [])
        items_previous = locations.get("previous", [])
        place_items_in_timeline(
            items_current,
            items_previous,
            locations_timeline,
            add_to_current=add_location_current,
            add_to_previous=add_location_previous,
            add_to_either=add_location_either,
        )

    def add_skill_current(skill_name):
        base_payload["skills"]["current"]["isSelected"].append(
            {"name": skill_name[0], "logic": skill_name[1]}
        )

    def add_skill_previous(skill_name):
        base_payload["skills"]["previous"]["isSelected"].append(
            {"name": skill_name[0], "logic": skill_name[1]}
        )

    def add_skill_either(skill_name):
        base_payload["skills"]["either"]["isSelected"].append(
            {"name": skill_name[0], "logic": skill_name[1]}
        )

    if skills and skills_timeline:
        items_current = skills.get("current", [])
        items_previous = skills.get("previous", [])
        place_items_in_timeline(
            items_current,
            items_previous,
            skills_timeline,
            add_to_current=add_skill_current,
            add_to_previous=add_skill_previous,
            add_to_either=add_skill_either,
        )

    if university:
        base_payload["university"]["current"]["isSelected"] = [
            {"name": item} for item in university
        ]

    if education:
        for item in education:
            degree = item.get("degree", None)
            major = item.get("major", None)
            temp = {}
            if degree:
                temp["degree"] = degree
            if major:
                temp["major"] = major

            base_payload["education"]["current"]["isSelected"].append(temp)

    return base_payload


async def run_l2_industry_expert(query: str, es_client: AsyncElasticsearch):
    try:
        prompt = await get_prompt_expert(
            query=query,
            expert_name="L2_INDUSTRY_EXPERT",
        )
    except Exception as e:
        logger.error("get_prompt_expert failed for query '%s': %s", query, str(e))
        return {}

    try:
        response = await get_expert_response(prompt)
    except Exception as e:
        logger.error("get_expert_response failed for L2 industry expert: %s", str(e))
        return {}

    parsed_response = extract_generic("<filters_json>", "</filters_json>", response)
    if not parsed_response:
        logger.error(
            "No filters extracted from L2 Industry Expert for query: %s", query
        )
        return {}

    try:
        return json.loads(parsed_response)
    except Exception as e:
        logger.error(
            "Invalid JSON in parsed L2 Industry Expert response: %s", parsed_response
        )
        return {}


async def generate_icp_query(
    description: str,
    key_titles: list[str],
    company_size: str,
    revenue: str,
    funding: str,
):
    system_prompt = QUERY_WRITER
    user_prompt = f"<initial_description>{description}</initial_description>\n"
    if company_size:
        user_prompt += f"<company_size>{company_size}</company_size>\n"
    if revenue:
        user_prompt += f"<revenue>{revenue}</revenue>\n"
    if funding:
        user_prompt += f"<funding>{funding}</funding>\n"

    message = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    try:
        res = await asynchronous_llm(
            model="gpt-4.1-mini",
            fallback_openai_model="gpt-4o",
            messages=message,
            temperature=0.2,
        )
        res = extract_generic("<final_description>", "</final_description>", res)
        return res
    except Exception as e:
        logger.error("Error in [Get Profiles From ICP] generate_icp_query: %s", str(e))
        return None


async def _get_query(description, key_titles, company_size, revenue, funding):
    if company_size or revenue or funding:
        query = await generate_icp_query(
            description, key_titles, company_size, revenue, funding
        )
        if not query:
            query = description
    else:
        query = description
    return query


async def get_profiles_from_icp(
    uuid: Optional[UUID],
    icp_json: Dict,
    es_client: AsyncElasticsearch,
    es_client_compressed: AsyncElasticsearch,
):
    description = icp_json.get("description", "")
    key_titles = icp_json.get("key_titles", [])
    company_size = icp_json.get("company_size", "")
    revenue = icp_json.get("revenue", "")
    funding = icp_json.get("funding", "")
    try:
        if not uuid:
            logger.info("[INFO] UUID not provided treating as new ICP")
            query = await _get_query(
                description, key_titles, company_size, revenue, funding
            )
            m2m_profile_ids = await get_profiles_from_l0(
                query, es_client, es_client_compressed
            )
            refined_m2m_profile_ids, company_profile_list, company_details_map = (
                await get_company_data_from_profiles(
                    m2m_profile_ids, es_client, es_client_compressed, True
                )
            )

            new_uuid = str(uuid_lib.uuid4())
            try:
                await upsert_cached_icp_profiles(
                    new_uuid, icp_json, refined_m2m_profile_ids, ICP_PROFILES_TABLE
                )
            except Exception as err:
                logger.error(f"Error {err}")

            uuid_to_return = new_uuid
        else:
            try:
                cached = await get_cached_icp_profiles(str(uuid), ICP_PROFILES_TABLE)
            except Exception as err:
                logger.error(err)

            if cached is None:
                logger.info("[INFO] UUID not found in DB – treating as new ICP")
                query = await _get_query(
                    description, key_titles, company_size, revenue, funding
                )
                m2m_profile_ids = await get_profiles_from_l0(
                    query, es_client, es_client_compressed
                )
                refined_m2m_profile_ids, company_profile_list, company_details_map = (
                    await get_company_data_from_profiles(
                        m2m_profile_ids, es_client, es_client_compressed, True
                    )
                )

                new_uuid = str(uuid_lib.uuid4())
                await upsert_cached_icp_profiles(
                    new_uuid, icp_json, refined_m2m_profile_ids, ICP_PROFILES_TABLE
                )
                uuid_to_return = new_uuid

            else:
                cached_profile_ids, created_at = cached
                if created_at is not None and created_at < datetime.now(
                    timezone.utc
                ) - timedelta(days=30):
                    logger.info(
                        "[INFO] UUID found but cache is old – re-fetching profiles"
                    )
                    query = await _get_query(
                        description, key_titles, company_size, revenue, funding
                    )
                    m2m_profile_ids = await get_profiles_from_l0(
                        query, es_client, es_client_compressed
                    )
                    (
                        refined_m2m_profile_ids,
                        company_profile_list,
                        company_details_map,
                    ) = await get_company_data_from_profiles(
                        m2m_profile_ids, es_client, es_client_compressed, True
                    )
                    await update_cached_profiles_only(
                        uuid, refined_m2m_profile_ids, ICP_PROFILES_TABLE
                    )
                    uuid_to_return = uuid
                else:
                    logger.info(
                        "[INFO] UUID found and cache is recent – returning cached data"
                    )
                    m2m_profile_ids = cached_profile_ids
                    (
                        refined_m2m_profile_ids,
                        company_profile_list,
                        company_details_map,
                    ) = await get_company_data_from_profiles(
                        m2m_profile_ids, es_client, es_client_compressed, False
                    )
                    uuid_to_return = uuid

        return uuid_to_return, company_profile_list, company_details_map

    except Exception as e:
        logger.error(
            "Error in [Get Profiles From ICP] get_profiles_from_icp: %s", str(e)
        )
        return "", [], {}


def select_profiles_with_quota_distribution(
    m2m_profile_ids, company_profile_list, company_details_map, max_profiles=75
):
    # Build map of profile ID -> score
    score_map = {
        d["id"]: d["score"] for d in m2m_profile_ids if "id" in d and "score" in d
    }

    experience_hits_map = {
        d["id"]: d["experienceHits"]
        for d in m2m_profile_ids
        if "id" in d and "experienceHits" in d
    }

    # Build map of profile ID -> company ID
    profile_to_company_map = {
        pid: company_data["company_id"]
        for company_data in company_profile_list
        if "company_id" in company_data and "profile_ids" in company_data
        for pid in company_data["profile_ids"]
    }

    # Collect stats per company for weighted selection
    company_stats = []
    for company_data in company_profile_list:
        company_id = company_data["company_id"]
        profile_ids = company_data.get("profile_ids", [])

        # Filter scores for the company's profiles
        scores = [
            score_map[pid]
            for pid in profile_ids
            if pid in score_map and score_map[pid] is not None
        ]

        avg_score = sum(scores) / len(scores) if scores else 0
        company_stats.append(
            {
                "company_id": company_id,
                "profile_ids": profile_ids,
                "num_profiles": len(profile_ids),
                "avg_score": avg_score,
            }
        )

    total_weight = sum(
        stat["avg_score"] for stat in company_stats if stat["avg_score"] > 0
    )

    selected_profile_ids = []

    # Allocate profiles proportionally based on avg_score
    for stat in company_stats:
        company_id = stat["company_id"]
        profile_ids = stat["profile_ids"]
        weighted_score = stat["avg_score"]

        share = (weighted_score / total_weight) if total_weight > 0 else 0
        num_to_select = max(1, math.floor(share * max_profiles))
        num_to_select = min(num_to_select, len(profile_ids))

        # Select top-scoring profiles for this company
        scored_profiles = [
            (pid, score_map[pid])
            for pid in profile_ids
            if pid in score_map and score_map[pid] is not None
        ]
        scored_profiles.sort(key=lambda x: x[1], reverse=True)

        top_profiles = [pid for pid, _ in scored_profiles[:num_to_select]]
        selected_profile_ids.extend(top_profiles)

    # Fill any remaining slots with top profiles across all companies
    remaining_slots = max_profiles - len(selected_profile_ids)
    if remaining_slots > 0:
        already_selected = set(selected_profile_ids)
        remaining_profiles = [
            (pid, score)
            for pid, score in score_map.items()
            if pid not in already_selected and score is not None
        ]
        remaining_profiles.sort(key=lambda x: x[1], reverse=True)
        extra_profiles = [pid for pid, _ in remaining_profiles[:remaining_slots]]
        selected_profile_ids.extend(extra_profiles)

    selected_profile_ids = selected_profile_ids[:max_profiles]

    selected_set = set(selected_profile_ids)

    refined_m2m_profile_ids = [
        {"id": pid, "score": score_map[pid], "experienceHits": experience_hits_map[pid]}
        for pid in selected_profile_ids
        if pid in score_map
    ]

    refined_company_profile_list = []
    retained_company_ids = set()

    for company_data in company_profile_list:
        company_id = company_data["company_id"]
        profile_ids = company_data.get("profile_ids", [])

        selected_profiles_for_company = [
            pid for pid in profile_ids if pid in selected_set
        ]

        if selected_profiles_for_company:
            refined_company_profile_list.append(
                {"company_id": company_id, "profile_ids": selected_profiles_for_company}
            )
            retained_company_ids.add(company_id)
    refined_company_details_map = {
        cid: details
        for cid, details in company_details_map.items()
        if cid in retained_company_ids
    }

    return (
        refined_m2m_profile_ids,
        refined_company_profile_list,
        refined_company_details_map,
    )


def remove_outliers_from_profiles(m2m_profile_ids, company_profile_list):
    score_map = {p["id"]: p["score"] for p in m2m_profile_ids}
    valid_profile_ids = set()

    lengths = np.array(
        [
            len(c.get("profile_ids", []))
            for c in company_profile_list
            if c.get("profile_ids")
        ]
    )
    min_len, max_len = lengths.min(), lengths.max()

    for company in company_profile_list:
        profile_ids = company.get("profile_ids", [])
        scored_profiles = [
            (pid, score_map[pid]) for pid in profile_ids if pid in score_map
        ]

        if len(scored_profiles) <= 1:
            valid_profile_ids.update(pid for pid, _ in scored_profiles)
            continue

        scores = sorted(score for _, score in scored_profiles)
        n = len(scores)
        mid = n // 2
        lower_half = scores[:mid]
        upper_half = scores[mid + 1 :] if n % 2 else scores[mid:]

        q1 = median(lower_half)
        q3 = median(upper_half)
        iqr = q3 - q1

        l = len(profile_ids)
        scaled = (l - min_len) / (max_len - min_len + 1e-6)
        multiplier = 0.5 + scaled * 1.5

        lower_bound = q1 - multiplier * iqr

        non_outliers = [
            (pid, score) for pid, score in scored_profiles if score >= lower_bound
        ]

        if not non_outliers:
            pid_max = max(scored_profiles, key=lambda x: x[1])[0]
            company["profile_ids"] = [pid_max]
            valid_profile_ids.add(pid_max)
        else:
            kept_ids = [pid for pid, _ in non_outliers]
            company["profile_ids"] = kept_ids
            valid_profile_ids.update(kept_ids)

    m2m_profile_ids[:] = [p for p in m2m_profile_ids if p["id"] in valid_profile_ids]
    return m2m_profile_ids, company_profile_list


def log_m2m_error(message: str, es_client: AsyncElasticsearch):
    write_logs(
        es_client=es_client,
        table_name="error_logs_dir",
        column_name="error_logs",
        payload={"feedback get_profiles_from_l0 m2m route error": message},
    )


async def get_profiles_from_l0(
    query: str, es_client: AsyncElasticsearch, es_client_compressed: AsyncElasticsearch
):
    try:
        raw_filters, (current_comps, past_comps, comp_gen_event) = await asyncio.gather(
            *[
                call_l0_filter_extraction(query, es_client=es_client),
                call_comp_gen_endpoint(query, es_client=es_client),
            ],
        )

        filters = await process_approaches([raw_filters])
        filters = filters[0]
        filters.update(
            {
                "companies": {"current": current_comps, "previous": past_comps},
                "companies_timeline": MAP_TIMELINE[comp_gen_event],
            }
        )

        payload = create_search_payload(**filters)

        for i in range(3):
            try:
                async with httpx.AsyncClient() as client:
                    start = time.perf_counter()
                    m2m_response = await client.post(
                        url=os.getenv("M2M_ROUTE", ""),
                        headers={"Authorization": os.getenv("M2M_BEARER", "")},
                        json=payload,
                        timeout=MAX_TIMEOUT,
                    )
                    end = time.perf_counter()
                    logging.info(f"M2M Route 01 took {end - start} seconds")

                if m2m_response.status_code != 200:
                    try:
                        response_data = m2m_response.json()
                    except Exception:
                        response_data = "Non-JSON response"

                    log_m2m_error(
                        f"Issue with m2m route, status = {m2m_response.status_code}, data = {response_data}",
                        es_client,
                    )

                    if i == 2:
                        log_m2m_error(
                            "Wasn't able to get profiles after 3 attempts", es_client
                        )
                        return []

                    log_m2m_error(f"Retrying after {i} seconds", es_client)
                    await asyncio.sleep(i)
                else:
                    break

            except httpx.RequestError as e:
                log_m2m_error(f"HTTP request failed on attempt {i+1}: {str(e)}")
                if i == 2:
                    log_m2m_error("Final attempt failed due to network error")
                    return []
                await asyncio.sleep(i)

        m2m_profile_ids = m2m_response.json()["profiles"]

        if len(m2m_profile_ids) == 0:
            start = time.perf_counter()
            company_expert_filters = await run_l2_industry_expert(query, es_client)
            end = time.perf_counter()
            logging.info(f"L2 Industry expert took {end - start} seconds")

            industry_filter = {
                "industry": company_expert_filters["filters"]["industry"]
            }

            cleaned_industry_filter = clean_results(industry_filter)
            reversed_industry_output = await reverse_output_to_input(
                cleaned_industry_filter
            )
            raw_filters.update(reversed_industry_output)

            processed_approaches = await process_approaches(
                approaches=[raw_filters],
            )
            filters = processed_approaches[0]
            filters.update(
                {
                    "companies": {"current": current_comps, "previous": past_comps},
                    "companies_timeline": MAP_TIMELINE[comp_gen_event],
                }
            )

            payload = create_search_payload(**filters)
            for i in range(3):
                async with httpx.AsyncClient() as client:
                    start = time.perf_counter()
                    m2m_response = await client.post(
                        url=os.getenv("M2M_ROUTE", ""),
                        headers={"Authorization": os.getenv("M2M_BEARER", "")},
                        json=payload,
                        timeout=MAX_TIMEOUT,
                    )
                    end = time.perf_counter()
                    logging.info(f"M2M Route 02 took {end - start} seconds")

                if m2m_response.status_code != 200:
                    if i == 2:
                        return []
                    await asyncio.sleep(i)
                else:
                    break

            m2m_profile_ids = m2m_response.json()["profiles"]

        if m2m_profile_ids:
            first = m2m_profile_ids[0]
            if "profile_id" in first:
                m2m_profile_ids = [
                    {
                        **p,
                        "id": p["profile_id"],
                        **{k: v for k, v in p.items() if k != "profile_id"},
                    }
                    for p in m2m_profile_ids
                ]
        return m2m_profile_ids

    except Exception as e:
        logger.error(
            "Error in [Get Profiles From ICP] get_profiles_from_l0: %s", str(e)
        )
        logger.error("Traceback: %s", traceback.format_exc())
        return []


async def get_company_data_from_profiles(
    m2m_profile_ids, es_client, es_client_compressed, profile_refinement
):
    if m2m_profile_ids:
        company_profile_list, company_details_map = (
            await get_unique_companies_from_matching_profiles(
                m2m_profile_ids, es_client, es_client_compressed
            )
        )

        if profile_refinement:
            m2m_profile_ids, company_profile_list = remove_outliers_from_profiles(
                m2m_profile_ids, company_profile_list
            )

            m2m_profile_ids, company_profile_list, company_details_map = (
                select_profiles_with_quota_distribution(
                    m2m_profile_ids,
                    company_profile_list,
                    company_details_map,
                    max_profiles=75,
                )
            )

    else:
        company_profile_list, company_details_map = [], {}

    urns = list(company_details_map.keys())
    final_lst = await get_additional_company_data(urns, es_client)

    universal_names = []

    for urn, details in company_details_map.items():
        universal_name = details.get("company_universal_name")
        if universal_name:
            universal_names.append(universal_name)
        enriched = final_lst.get(universal_name, {})

        details.update(
            {
                "revenue": enriched.get("revenue"),
                "funding": enriched.get("funding"),
                "company_location": enriched.get("company_location"),
            }
        )

    company_ownership_data = await get_company_ownership_data(
        universal_names, es_client
    )

    for urn, details in company_details_map.items():
        universal_name = details.get("company_universal_name")
        if universal_name:
            details.update(
                {"ownership_types": company_ownership_data.get(universal_name, [])}
            )

    return m2m_profile_ids, company_profile_list, company_details_map
