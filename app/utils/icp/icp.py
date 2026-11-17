import asyncio
import os
from typing import List
import logging
from typing import List, Dict, Any

from elasticsearch import AsyncElasticsearch

from app.database.utilities import postgres_fetch_all
from app.utils.icp.prompts import ICP_GENERATION_SYSTEM_PROMPT
from app.utils.util_functions import extract_generic, get_company_ownership_data
from qutils.llm.utilities import asynchronous_llm

logger = logging.getLogger(__name__)


async def get_icp(
    list_of_top_5_profiles: List[List[dict]], es_client: AsyncElasticsearch
):
    tasks = [
        generate_icp(top_5_profiles, es_client)
        for top_5_profiles in list_of_top_5_profiles
    ]
    res = await asyncio.gather(*tasks)
    return res


async def generate_icp(top_5_profiles: List[dict], es_client: AsyncElasticsearch):

    cluster_profiles = await map_profile_data(top_5_profiles, es_client)

    system_prompt = ICP_GENERATION_SYSTEM_PROMPT
    user_prompt = cluster_profiles
    chat = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    res = await asynchronous_llm(
        messages=chat, temperature=0.3, model="gpt-4.1", fallback_openai_model="gpt-4o"
    )

    res = extract_generic("<icps>", "</icps>", res)

    res = eval(res)

    for icp in res:
        if "company_revenue" in icp:
            icp["revenue"] = icp.pop("company_revenue")
        if "company_funding" in icp:
            icp["funding"] = icp.pop("company_funding")

    return res


async def get_stock_symbols(urns: List[str], es_client: AsyncElasticsearch):
    data = {}
    urns = [
        f"urn:li:fsd_company:{i}"
        for i in urns
        if i and "urn:li:fsd_company:" not in str(i)
    ]
    res = await es_client.search(
        index=os.getenv("ES_COMPANIES_INDEX"),
        body={
            "query": {"terms": {"li_urn": urns}},
            "_source": [
                "cb_stock_symbol",
                "li_universalname",
                "li_urn",
                "li_name",
                "li_confirmedlocations",
                "li_size",
                "li_staffcount",
                "li_industries",
                "li_specialties",
                "li_headquarter",
            ],
            "size": len(urns),
        },
    )
    for result in res["hits"]["hits"]:
        es_data = result["_source"]
        li_size = es_data.get("li_size", "")
        li_staffcount = es_data.get("li_staffcount", "")

        company_size = None

        if li_size and not li_staffcount:
            company_size = li_size
        elif li_staffcount and not li_size:
            company_size = li_staffcount
        else:
            if li_size and li_staffcount:
                company_size = li_size if li_size > li_staffcount else li_staffcount
            else:
                company_size = None
        li_industries = es_data.get("li_industries", [])
        li_specialties = es_data.get("li_specialties", [])
        if isinstance(li_industries, str):
            li_industries = [li_industries]
        if isinstance(li_specialties, str):
            li_specialties = [li_specialties]

        data[int(es_data["li_urn"].split(":")[-1])] = {
            "cb_stock_symbol": es_data.get("cb_stock_symbol", ""),
            "li_universalname": es_data.get("li_universalname", ""),
            "li_urn": es_data.get("li_urn", ""),
            "li_name": es_data.get("li_name", ""),
            "li_confirmedlocations": es_data.get("li_confirmedlocations", []),
            "company_size": company_size,
            "li_industries": li_industries,
            "li_specialties": li_specialties,
            "li_headquarter": (
                es_data.get("li_headquarter", {})
                if es_data.get("li_headquarter", {})
                else {}
            ),
        }

    return data


async def get_companies_revenue(cb_stock_symbols: List[str]):
    cb_stock_symbols = [i for i in cb_stock_symbols if i]
    if not cb_stock_symbols:
        return {}
    cb_stock_symbols_q = ", ".join(f"'{symbol}'" for symbol in cb_stock_symbols)
    query = f"""
    SELECT stock, ARRAY_AGG(ROW(calendar_year, value_in_usd) ORDER BY calendar_year DESC) AS revenue 
    FROM fmp_financial_statements_mv
    WHERE stock in ({cb_stock_symbols_q})
    AND metric = 'revenue'
    GROUP BY stock
    """
    res = await postgres_fetch_all(query)
    if res:
        res = {i[0]: i[1] for i in res}
    else:
        res = {}

    return res


async def get_latest_funding_round_data(li_universalnames: List[str]):
    li_universalnames = [i for i in li_universalnames if i]

    if not li_universalnames:
        return {}
    li_universalnames_q = ", ".join(f"'{name}'" for name in li_universalnames)

    query = f"""SELECT DISTINCT ON (cb.li_universalname)
        cb.li_universalname,
        fr.total_funding_amount_currency_in_usd
    FROM
        funding_rounds AS fr
    JOIN
        cb_basic_company_data AS cb
        ON cb.cb_url = fr.organization_name_url
    WHERE
        cb.li_universalname IN ({li_universalnames_q})
    ORDER BY
        cb.li_universalname,
        fr.announced_date DESC;"""

    try:
        res = await postgres_fetch_all(query)
    except Exception as err:
        logger.error("Error executing the latest funding round data query")
        return {}

    if res:
        res = {i[0]: i[1] for i in res}
    else:
        res = {}

    return res


async def get_last_three_funding_rounds_data(li_universalnames: List[str]):
    li_universalnames = [i for i in li_universalnames if i]

    if not li_universalnames:
        return {}

    li_universalnames_q = ", ".join(f"'{name}'" for name in li_universalnames)

    query = f"""
    SELECT 
        cbcd.li_universalname, 
        ARRAY_AGG(
            ROW(
                fr.announced_date, 
                fr.funding_type::text, 
                fr.money_raised_currency_in_usd, 
                fr.funding_stage::text, 
                fr.total_funding_amount_currency_in_usd, 
                fr.number_of_funding_rounds
            )
            ORDER BY fr.announced_date DESC
        ) AS funding_rounds
    FROM 
        funding_rounds fr
    INNER JOIN 
        cb_basic_company_data cbcd 
        ON fr.organization_name_url = cbcd.cb_url
    WHERE 
        cbcd.li_universalname IN ({li_universalnames_q})
    GROUP BY 
        cbcd.li_universalname;
    """

    res = await postgres_fetch_all(query)
    if res:
        res = {row[0]: (row[1][:3],) if row[1] else ([],) for row in res}
    else:
        res = {}
    return res


def format_number(value):
    """
    Format a numeric value into abbreviated string format with K, M, B suffixes.

    Args:
        value (int/float): The numeric value to format

    Returns:
        str: Formatted string like $10K, $100K, $1M, etc.
    """
    if not value:
        return None
    if not isinstance(value, (int, float)):
        raise ValueError("Input must be a numeric value")

    # Handle negative numbers
    is_negative = value < 0
    value = abs(value)

    # Define thresholds and suffixes
    if value >= 1_000_000_000:  # Billions
        formatted = f"${round(value / 1_000_000_000)}B"
    elif value >= 1_000_000:  # Millions
        formatted = f"${round(value / 1_000_000)}M"
    elif value >= 1_000:  # Thousands
        formatted = f"${round(value / 1_000)}K"
    else:  # Less than 1000
        formatted = f"${round(value)}"

    # Add negative sign if needed
    if is_negative:
        formatted = "-" + formatted

    return formatted


async def get_additional_company_data(
    urns: List[str], es_client: AsyncElasticsearch, is_company_data=False
):
    stock_symbols = await get_stock_symbols(urns, es_client)

    universalname_to_urn = {
        stock_symbols[i]["li_universalname"]: i for i in stock_symbols
    }

    company_locations = {
        stock_symbols[i]["li_universalname"]: stock_symbols[i]["li_headquarter"]
        for i in stock_symbols
        if stock_symbols[i]["li_universalname"]
    }

    for i in company_locations:
        location = company_locations[i]
        if location:
            if isinstance(location, dict):
                city = location.get("city", "")
                country = location.get("country", "")
                if city and country:
                    location = f"{city}, {country}"
                elif city:
                    location = city
                elif country:
                    location = country
            elif isinstance(location, str):
                location = location
        else:
            location = ""
        company_locations[i]["company_location"] = location

    company_sizes = {
        stock_symbols[i]["li_universalname"]: stock_symbols[i]["company_size"]
        for i in stock_symbols
        if stock_symbols[i]["li_universalname"]
    }

    li_universalnames = [
        stock_symbols[i]["li_universalname"]
        for i in stock_symbols
        if stock_symbols[i]["li_universalname"]
    ]

    public_companies = {
        stock_symbols[i]["cb_stock_symbol"]: stock_symbols[i]["li_universalname"]
        for i in stock_symbols
        if stock_symbols[i]["cb_stock_symbol"]
    }

    private_companies = [
        stock_symbols[i]["li_universalname"]
        for i in stock_symbols
        if not stock_symbols[i]["cb_stock_symbol"]
    ]

    """fr.announced_date,
    fr.total_funding_amount_currency_in_usd,
    fr.number_of_funding_rounds"""

    company_revenue, funding_data = await asyncio.gather(
        get_companies_revenue(public_companies),
        get_latest_funding_round_data(private_companies),
    )

    for comp in funding_data:
        if funding_data[comp]:
            funding_amount = format_number(funding_data[comp])
            funding_data[comp] = funding_amount
        else:
            funding_data[comp] = None

    # Reverse public_companies to map universal_name → stock_symbol
    universalname_to_stock = {v: k for k, v in public_companies.items()}

    for comp in company_revenue:
        for revenue in company_revenue[comp]:
            if revenue[1]:
                company_revenue[comp] = revenue[1]
                break

    revenue_data = {
        public_companies[symbol]: format_number(revenue)
        for symbol, revenue in company_revenue.items()
        if symbol in public_companies
    }

    final_lst = {}

    if is_company_data:
        ownership_data = await get_company_ownership_data(li_universalnames, es_client)
        for name in li_universalnames:
            final_lst[universalname_to_urn[name]] = {
                "company": stock_symbols[universalname_to_urn[name]].get("li_name"),
                "company_urn": universalname_to_urn[name],
                "company_universal_name": name,
                "revenue": revenue_data.get(name),
                "funding": funding_data.get(name),
                "company_size": company_sizes.get(name),
                "li_industries": stock_symbols[universalname_to_urn[name]].get(
                    "li_industries"
                ),
                "company_location": company_locations[name].get("company_location"),
                "ownership_types": ownership_data.get(name, []),
            }
    else:
        for name in li_universalnames:
            final_lst[name] = {
                "revenue": revenue_data.get(name),
                "funding": funding_data.get(name),
                "company_location": company_locations[name].get("company_location"),
            }

    return final_lst


async def get_company_data(urns: List[str], es_client: AsyncElasticsearch):
    stock_symbols = await get_stock_symbols(urns, es_client)
    li_universalnames = [
        stock_symbols[i]["li_universalname"]
        for i in stock_symbols
        if stock_symbols[i]["li_universalname"]
    ]
    public_companies = {
        stock_symbols[i]["cb_stock_symbol"]: stock_symbols[i]["li_universalname"]
        for i in stock_symbols
        if stock_symbols[i]["cb_stock_symbol"]
    }
    private_companies = [
        stock_symbols[i]["li_universalname"]
        for i in stock_symbols
        if not stock_symbols[i]["cb_stock_symbol"]
    ]
    company_revenue, funding_data = await asyncio.gather(
        get_companies_revenue(public_companies),
        get_last_three_funding_rounds_data(private_companies),
    )

    revenue_data = {}

    for i in company_revenue:
        revenue_data[public_companies[i]] = company_revenue[i]

    final_lst = {
        i: {"revenue": revenue_data.get(i, None), "funding": funding_data.get(i, None)}
        for i in li_universalnames
    }

    res = map_company_data(final_lst)

    return res, stock_symbols


def map_company_data(company_data: dict):
    for i in company_data:
        company_str = ""
        revenue_str = ""
        funding_str = ""
        if company_data[i]["revenue"]:
            for revenue in company_data[i]["revenue"]:
                if revenue[1]:
                    revenue_str += f"<revenue_for_{revenue[0]}> {revenue[1]} USD </revenue_for_{revenue[0]}>\n"
        if company_data[i]["funding"]:
            for idx, funding in enumerate(company_data[i]["funding"][0][:3], 1):
                funding_str += f"<funding_round_{idx}>\n"
                if funding[0]:
                    funding_str += f"<funding_date> {funding[0]} </funding_date>\n"
                if funding[1]:
                    funding_str += f"<funding_type> {funding[1]} </funding_type>\n"
                if funding[2]:
                    funding_str += f"<funding_amount> {funding[2]} </funding_amount>\n"
                if funding[3]:
                    funding_str += f"<funding_stage> {funding[3]} </funding_stage>\n"
                if funding[4]:
                    if funding[5] and funding[5] > 1:
                        funding_str += f"<total_funding_across_{funding[5]}_rounds> {funding[4]} USD </total_funding_across_{funding[5]}_rounds>\n"
                funding_str += f"</funding_round_{idx}>\n"

        if revenue_str or funding_str:
            company_str += revenue_str + funding_str
            company_data[i] = company_str

    return company_data


def convert_days_to_years_and_months(days: float):
    years = int(days / 365)
    months = int((days % 365) / 30)
    return f"{years} years {months} months"


async def map_profile_data(top_5_profiles: List[dict], es_client: AsyncElasticsearch):
    urns = []
    for profile in top_5_profiles:
        urns.append(profile["company_urn"])
    urns = list(set(urns))

    company_data, stock_symbols = await get_company_data(urns, es_client)

    cluster_profiles = ""

    for idx, profile in enumerate(top_5_profiles, 1):
        profile_str = ""
        if profile["job_title"]:
            profile_str += f"<job_title> {profile['job_title']} </job_title>\n"

        if profile["company_name"]:
            profile_str += f"<company_name> {profile['company_name']} </company_name>\n"

        elif profile["company_urn"] and profile["company_urn"] in stock_symbols:
            if stock_symbols[profile["company_urn"]]["li_name"]:
                profile_str += f"<company_name> {stock_symbols[profile['company_urn']]['li_name']} </company_name>\n"

        comp_size = 0
        if profile["company_size"]:
            profile_str += f"<company_size> {profile['company_size']} </company_size>\n"
            comp_size = profile["company_size"]

        if comp_size and comp_size < 1000:
            if profile["company_industry"] or profile["company_speciality"]:
                if isinstance(profile["company_industry"], str):
                    profile["company_industry"] = [profile["company_industry"]]
                if isinstance(profile["company_speciality"], str):
                    profile["company_speciality"] = [profile["company_speciality"]]

                profile["company_speciality"] = [
                    i for i in profile["company_speciality"] if i and len(i) < 50
                ]

                combined_industries = list(
                    set(profile["company_industry"] + profile["company_speciality"])
                )
                company_industries = ", ".join(combined_industries)

                profile_str += (
                    f"<company_industry> {company_industries} </company_industry>\n"
                )
            company_info = stock_symbols.get(profile["company_urn"])
            if company_info and company_info.get("li_confirmedlocations"):
                locations = set()
                for location in company_info["li_confirmedlocations"]:
                    if location.get("country"):
                        locations.add(location["country"])
                    elif location.get("geographicArea"):
                        locations.add(location["geographicArea"])
                    elif location.get("city"):
                        locations.add(location["city"])

                locations = ", ".join(locations)

                profile_str += f"<company_location> {locations} </company_location>\n"

            if profile["company_description"]:
                profile_str += f"<company_description> {profile['company_description']} </company_description>\n"
        if company_data.get(profile["company_universal_name"]):
            financial = company_data[profile["company_universal_name"]]
            if financial:
                profile_str += f"{financial}\n"
        elif stock_symbols.get(profile["company_urn"]):
            revenue = stock_symbols[profile["company_urn"]].get("revenue", None)
            funding = stock_symbols[profile["company_urn"]].get("funding", None)
            if revenue:
                profile_str += f"<company_revenue> {revenue} </company_revenue>\n"
            if funding:
                profile_str += f"<company_funding> {funding} </company_funding>\n"

        if profile["job_summary"]:
            profile_str += f"<job_summary> {profile['job_summary']} </job_summary>\n"

        cluster_profiles += f"<profile_{idx}>\n{profile_str}</profile_{idx}>\n"

    return cluster_profiles
