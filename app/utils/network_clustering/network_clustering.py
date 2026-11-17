import asyncio
import json
import logging
import os
import random
import re
import string
from collections import Counter, defaultdict
from typing import List

import numpy as np
import networkx as nx
from elasticsearch import AsyncElasticsearch
from nltk.util import bigrams
from openai import AsyncClient
from sklearn.cluster import KMeans
from sklearn.metrics.pairwise import cosine_similarity

from app.utils.network_clustering.clustering_utils import (
    parse_llm_merge_response,
    GENERIC_INDUSTRY_WATCH_LIST,
    TITLE_NORMALIZATION_MAP,
)
from app.utils.network_clustering.prompts import (
    CLUSTERING_DETAILS_SYSTEM_PROMPT,
    CLUSTER_MERGE_PROMPT,
)
from app.utils.util_functions import extract_generic
from qutils.llm.utilities import asynchronous_llm

logger = logging.getLogger(__name__)


async def get_profile_data(ids, es_client, retry_count=0):
    data = {}
    tasks = [
        es_client.search(
            body={
                "_source": [
                    "experience",
                    "public_identifier",
                    "_id",
                    "total_tenure",
                    "location",
                ],
                "query": {"terms": {"public_identifier": ids[i : i + 250]}},
            },
            index=os.getenv("ES_PROFILES_INDEX"),
            size=250,
        )
        for i in range(0, len(ids), 250)
    ]

    try:
        combined_results = await asyncio.gather(*tasks)
        for results in combined_results:
            for result in results["hits"]["hits"]:
                public_identifier = result["_source"]["public_identifier"]
                data[public_identifier] = {
                    "experience": result["_source"].get("experience", []),
                    "skills": result["_source"].get("skills", []),
                    "_id": result["_id"],
                }
        if len(data) == 0:
            if retry_count < 3:
                await asyncio.sleep(1)
                data = await get_profile_data(ids, es_client, retry_count + 1)
    except Exception as e:
        print(e)
        if retry_count < 3:
            await asyncio.sleep(1)
            data = await get_profile_data(ids, es_client, retry_count + 1)
        else:
            data = {}

    return data


async def embed(doc: list, model="text-embedding-3-large", batch_size=2000):
    client = AsyncClient()
    tasks = []
    try:
        for i in range(0, len(doc), batch_size):
            batch = doc[i : i + batch_size]
            tasks.append(client.embeddings.create(input=batch, model=model))
        embeddings = await asyncio.gather(*tasks)
        embeddings = [emb.embedding for i in embeddings for emb in i.data]

        return np.array(embeddings)
    except Exception as e:
        logger.error(f"EXCEPTION IN EMBEDDING = doc = {e}")

        return None


def normalize_job_title(title: str) -> str:
    title = title.lower()
    title = re.sub(r"[^\w\s]", " ", title)
    title = re.sub(r"\s+", " ", title).strip()

    # Tokenize
    words = title.split()

    # Optional: handle multi-word keys in the map (e.g. "chief exec")
    i = 0
    normalized_words = []
    while i < len(words):
        matched = False
        # Check for 3-word, 2-word phrases
        for window in (3, 2):
            if i + window <= len(words):
                phrase = " ".join(words[i : i + window])
                if phrase in TITLE_NORMALIZATION_MAP:
                    normalized_words.append(TITLE_NORMALIZATION_MAP[phrase])
                    i += window
                    matched = True
                    break
        if not matched:
            word = words[i]
            normalized_words.append(TITLE_NORMALIZATION_MAP.get(word, word))
            i += 1

    return " ".join(normalized_words)


GENERIC_FIRST_WORDS = {"senior", "associate", "junior", "lead"}


async def cluster_profiles(res):
    filtered_res = {}
    jt = []
    profile_ids = []

    for pid, data in res.items():
        experience = data.get("experience")
        if experience and isinstance(experience, list) and len(experience) > 0:
            title = experience[0].get("title")
            if title:
                jt.append(title)
                profile_ids.append(pid)
            filtered_res[pid] = data

    res = filtered_res

    unique_job_titles = list(set(i.lower().strip() for i in jt))
    job_titles = unique_job_titles

    def clean_text(text):
        return normalize_job_title(text)

    cleaned_titles = [clean_text(title) for title in job_titles]

    all_words = []
    for title in cleaned_titles:
        all_words.extend(title.split())

    word_counts = Counter(all_words)

    frequencies = list(word_counts.values())
    mean_freq = np.mean(frequencies)
    std_freq = np.std(frequencies)

    threshold_strict = round(mean_freq + 0.2 * std_freq)

    generic_words = {"and", "of", "the", "a", "an"}

    common_words_strict = {
        word
        for word, count in word_counts.items()
        if count >= threshold_strict and word not in generic_words
    }

    simplified_titles_strict = []
    original_to_simplified_strict = {}

    for i, title in enumerate(job_titles):
        clean_title = cleaned_titles[i]
        common_words_in_title = [
            word for word in clean_title.split() if word in common_words_strict
        ]

        if common_words_in_title:
            if len(common_words_in_title) > 3:
                first_word = common_words_in_title[0]
                if first_word in GENERIC_FIRST_WORDS:
                    simplified_title = " ".join(common_words_in_title[1:4])
                else:
                    simplified_title = " ".join(common_words_in_title[:3])
            else:
                simplified_title = " ".join(common_words_in_title)
            simplified_titles_strict.append(simplified_title)
            original_to_simplified_strict[job_titles[i]] = simplified_title
        else:
            original_to_simplified_strict[job_titles[i]] = "(no common words)"

    final_set = list(set(simplified_titles_strict))

    embeddings = await embed(final_set)

    simplified_title_to_idx = {title: idx for idx, title in enumerate(final_set)}

    profile_to_embedding = {}
    new_profile_ids = []

    for pid, original_title in zip(profile_ids, jt):
        key = original_title.lower().strip()
        simplified = original_to_simplified_strict.get(key, "(no common words)")
        if simplified != "(no common words)" and simplified in simplified_title_to_idx:
            idx = simplified_title_to_idx[simplified]
            profile_to_embedding[pid] = embeddings[idx]
            new_profile_ids.append(pid)
            res[pid]["experience"][0]["simplified_title"] = simplified

    res = {pid: res[pid] for pid in new_profile_ids}

    profile_ids_list = new_profile_ids
    profile_embeddings = np.array(
        [profile_to_embedding[pid] for pid in profile_ids_list]
    )

    def cluster_by_similarity_threshold(embeddings, profile_ids, threshold=0.6):
        similarity_matrix = cosine_similarity(embeddings)
        clusters = defaultdict(list)
        assigned = set()
        cluster_id = 0

        for i in range(len(profile_ids)):
            if i in assigned:
                continue

            clusters[cluster_id].append(profile_ids[i])
            assigned.add(i)

            for j in range(len(profile_ids)):
                if (
                    j != i
                    and j not in assigned
                    and similarity_matrix[i, j] >= threshold
                ):
                    clusters[cluster_id].append(profile_ids[j])
                    assigned.add(j)

            cluster_id += 1
        return dict(clusters)

    clusters = cluster_by_similarity_threshold(
        profile_embeddings, profile_ids_list, threshold=0.6
    )

    sorted_clusters = sorted(clusters.items(), key=lambda x: len(x[1]), reverse=True)

    top_5_clusters = sorted_clusters[:5]

    top_profiles_set = set()
    for cid, members in top_5_clusters:
        top_profiles_set.update(members)

    res = {pid: res[pid] for pid in res if pid in top_profiles_set}

    cluster_stats = {}

    # Invert mapping: simplified title for each profile ID
    pid_to_simplified = {
        pid: res[pid]["experience"][0]["simplified_title"]
        for pid in res
        if "simplified_title" in res[pid]["experience"][0]
    }

    # Collect simplified titles for each cluster
    for cluster_id, profile_ids in top_5_clusters:
        titles = set()
        for pid in profile_ids:
            simplified = pid_to_simplified.get(pid)
            if simplified:
                titles.add(simplified)
        cluster_stats[f"cluster_{cluster_id}"] = list(titles)

    return res, top_5_clusters


def summarize_words_and_bigrams(titles, top_k=3):
    words = []
    bigram_list = []

    for title in titles:
        tokens = title.lower().strip().split()
        if not tokens:
            continue
        words.extend(tokens)
        bigram_list.extend([" ".join(b) for b in bigrams(tokens)])

    word_counts = Counter(words)
    bigram_counts = Counter(bigram_list)

    return {
        "simplified": titles,
        "most_frequent_words": word_counts.most_common(top_k),
        "most_frequent_bigrams": bigram_counts.most_common(top_k),
    }


def clean_description_text(text):
    if text:
        text = text.lower()
        text = re.sub(r"http\S+|www\S+|https\S+", "", text)
        text = re.sub(r"\S+@\S+", "", text)
        text = re.sub(r"<.*?>", " ", text)
        text = re.sub(r"[\n\t\r]+", " ", text)
        text = re.sub(r"[^a-z0-9.,!? ]+", " ", text)
        text = re.sub(r"([.!?]){2,}", r"\1", text)
        text = re.sub(r"\b\d+\b", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        text = text.encode("ascii", errors="ignore").decode()
    return text


def truncate_description(text: str, limit=1000):
    return text[:limit] if text else ""


# def save_summary_stats(cluster_hierarchy_name_map, res):
#     summary_a = defaultdict(lambda: {"num_profiles": 0, "unique_industry_lists": set()})
#     summary_ab = defaultdict(lambda: {"num_profiles": 0, "unique_industry_lists": set()})
#     summary_abc = {}

#     for final_key, profile_ids in cluster_hierarchy_name_map.items():
#         industries_set = set()

#         for pid in profile_ids:
#             exp = res[pid]["experience"][0]
#             industries = exp.get("company_industry", "")
#             if not industries:
#                 continue

#             ordered = tuple(sorted(industry.strip() for industry in industries.split(",") if industry.strip()))
#             industries_set.add(ordered)

#         a = final_key[:2]      # e.g., a1
#         ab = final_key[:4]     # e.g., a1b2
#         abc = final_key        # e.g., a1b2c3

#         summary_a[a]["num_profiles"] += len(profile_ids)
#         summary_a[a]["unique_industry_lists"].update(industries_set)

#         summary_ab[ab]["num_profiles"] += len(profile_ids)
#         summary_ab[ab]["unique_industry_lists"].update(industries_set)

#         summary_abc[abc] = {
#             "num_profiles": len(profile_ids),
#             "unique_industry_lists": sorted([list(inds) for inds in industries_set])
#         }

#     # Convert sets to sorted lists for JSON serialization
#     summary_json = {
#         "level_a": {
#             k: {
#                 "num_profiles": v["num_profiles"],
#                 "unique_industry_lists": sorted([list(inds) for inds in v["unique_industry_lists"]])
#             }
#             for k, v in summary_a.items()
#         },
#         "level_ab": {
#             k: {
#                 "num_profiles": v["num_profiles"],
#                 "unique_industry_lists": sorted([list(inds) for inds in v["unique_industry_lists"]])
#             }
#             for k, v in summary_ab.items()
#         },
#         "level_abc": dict(sorted(summary_abc.items(), key=lambda x: x[1]["num_profiles"], reverse=True))
#     }


async def sub_cluster_profiles(res, top_5_clusters):
    removed_count = 0

    for profile in list(res.keys()):
        exp = res[profile]["experience"][0]

        if not exp.get("company_industry"):
            del res[profile]
            removed_count += 1
            continue

        industries = sorted(exp["company_industry"])
        exp["company_industry"] = ", ".join(industries)
        exp["industry_sentence"] = f"{exp['company_industry']}"

        if exp.get("company_description"):
            exp["company_description"] = clean_description_text(
                exp["company_description"][:200]
            )

    # === Step 1: Embeddings ===
    industry_sentence_to_pids = defaultdict(list)
    for pid, data in res.items():
        sentence = data["experience"][0]["industry_sentence"]
        industry_sentence_to_pids[sentence].append(pid)

    unique_industry_sentences = list(industry_sentence_to_pids.keys())
    industry_embeddings = await embed(unique_industry_sentences)
    industry_text_to_index = {
        text: i for i, text in enumerate(unique_industry_sentences)
    }

    desc_text_to_pids = defaultdict(list)
    for pid, data in res.items():
        desc = (data["experience"][0].get("company_description") or "").strip()
        if desc:
            desc_text_to_pids[desc].append(pid)

    unique_descs = list(desc_text_to_pids.keys())
    desc_embeddings = await embed(unique_descs)
    desc_text_to_index = {text: i for i, text in enumerate(unique_descs)}

    # === Step 2: Main Clustering Logic ===
    subclusters_by_cluster = {}
    cluster_hierarchy_name_map = {}

    for cluster_idx, (cluster_id, profile_ids) in enumerate(top_5_clusters):
        a_label = f"a{cluster_idx+1}"

        # --- LEVEL B CLUSTERING (BY INDUSTRY) ---
        valid_pids = []
        industry_vectors = []
        for pid in profile_ids:
            data = res.get(pid)
            if not data:
                continue
            sentence = data["experience"][0].get("industry_sentence")
            if sentence not in industry_text_to_index:
                continue
            idx = industry_text_to_index[sentence]
            industry_vectors.append(industry_embeddings[idx])
            valid_pids.append(pid)

        if not industry_vectors:
            continue

        threshold = 0.8
        G = nx.Graph()
        G.add_nodes_from(range(len(valid_pids)))
        similarity_matrix = cosine_similarity(industry_vectors)
        for i in range(len(valid_pids)):
            for j in range(i + 1, len(valid_pids)):
                if similarity_matrix[i, j] >= threshold:
                    G.add_edge(i, j)
        components = list(nx.connected_components(G))
        industry_subclusters = {
            sub_id: [valid_pids[i] for i in component]
            for sub_id, component in enumerate(components)
        }

        # 1. Retain only the top 4 largest Level B clusters for processing
        top_4_b_clusters = dict(
            sorted(industry_subclusters.items(), key=lambda x: len(x[1]), reverse=True)[
                :4
            ]
        )

        final_c_level_clusters = []
        for b_id, pids in top_4_b_clusters.items():

            # 2. Check if the cluster's dominant industry is on the watch list
            industry_counts = Counter(
                res[pid]["experience"][0]["industry_sentence"] for pid in pids
            )
            dominant_industry = industry_counts.most_common(1)[0][0]

            # Use exact match for simplicity and robustness
            is_generic = any(
                watch_item in dominant_industry
                for watch_item in GENERIC_INDUSTRY_WATCH_LIST
            )

            # Practicality Filter: only cluster if the group is large enough
            if is_generic and len(pids) >= 10:
                # 3. If generic, sub-cluster by company description using K-Means
                desc_profiles = []
                desc_vectors = []
                for pid in pids:
                    desc = (
                        res[pid]["experience"][0].get("company_description") or ""
                    ).strip()
                    idx = desc_text_to_index.get(desc)
                    if idx is not None:
                        desc_profiles.append(pid)
                        desc_vectors.append(desc_embeddings[idx])

                if (
                    len(desc_profiles) >= 2
                ):  # Can only cluster if there are at least 2 profiles with descriptions

                    # Dynamically choose k=2 or k=3 based on the size of the group
                    num_niches = 3 if len(desc_profiles) > 40 else 2

                    kmeans = KMeans(n_clusters=num_niches, random_state=42, n_init=10)
                    labels = kmeans.fit_predict(desc_vectors)

                    # Group profiles by their new niche label
                    niche_clusters = defaultdict(list)
                    for i, label in enumerate(labels):
                        niche_clusters[label].append(desc_profiles[i])

                    # Add these new niche clusters to our final list
                    for niche_pids in niche_clusters.values():
                        final_c_level_clusters.append(niche_pids)
                else:
                    # Not enough data to sub-cluster, so we keep the original cluster
                    final_c_level_clusters.append(pids)
            else:
                # If not generic, we don't sub-cluster. Just keep the original cluster.
                final_c_level_clusters.append(pids)

        final_subclusters = defaultdict(list)
        final_id = 0
        for pids in final_c_level_clusters:

            c_label = f"c{final_id+1}"
            key = f"{a_label}{c_label}"
            cluster_hierarchy_name_map[key] = pids
            final_subclusters[final_id] = pids
            final_id += 1

        subclusters_by_cluster[cluster_id] = dict(final_subclusters)

    # === Step 4: Selection of Top Subclusters ===
    all_subclusters = []
    for cluster_id, subclusters in subclusters_by_cluster.items():
        for sub_id, profile_ids in subclusters.items():
            all_subclusters.append(
                {
                    "key": f"cluster{cluster_id}_sub{sub_id}",
                    "cluster_id": cluster_id,
                    "subcluster_id": sub_id,
                    "profile_ids": profile_ids,
                }
            )

    all_subclusters.sort(key=lambda x: len(x["profile_ids"]), reverse=True)

    global_top_3 = all_subclusters[:3]
    selected_subclusters = global_top_3.copy()
    selected_keys = {
        (entry["cluster_id"], entry["subcluster_id"]) for entry in global_top_3
    }
    represented_a_clusters = set(entry["cluster_id"] for entry in global_top_3)

    for cluster_id, subclusters in subclusters_by_cluster.items():
        if cluster_id in represented_a_clusters:
            continue
        subcluster_entries = [
            {
                "key": f"cluster{cluster_id}_sub{sub_id}",
                "cluster_id": cluster_id,
                "subcluster_id": sub_id,
                "profile_ids": profile_ids,
            }
            for sub_id, profile_ids in subclusters.items()
        ]
        if not subcluster_entries:
            continue
        subcluster_entries.sort(key=lambda x: len(x["profile_ids"]), reverse=True)
        for entry in subcluster_entries:
            key_tuple = (entry["cluster_id"], entry["subcluster_id"])
            if key_tuple not in selected_keys:
                selected_subclusters.append(entry)
                selected_keys.add(key_tuple)
                represented_a_clusters.add(cluster_id)
                break

    for entry in all_subclusters:
        if len(selected_subclusters) >= 7:
            break
        key_tuple = (entry["cluster_id"], entry["subcluster_id"])
        if key_tuple not in selected_keys:
            selected_subclusters.append(entry)
            selected_keys.add(key_tuple)

    final_output = []
    final_output_sizes_summaries = []

    for entry in selected_subclusters:
        subcluster_data = []
        company_size_title_list = []

        for pid in entry["profile_ids"]:
            data = res.get(pid)
            if not data:
                continue

            exp = data.get("experience", [{}])[0]
            company_size = exp.get("company_size", None)
            simplified_title = exp.get("simplified_title", "")

            if company_size:
                company_size_title_list.append((company_size, simplified_title))

            subcluster_data.append(
                {
                    "es_id": data.get("_id"),
                    "profile_id": pid,
                    "company_industry": exp.get("company_industry", []),
                    "company_description": exp.get("company_description", ""),
                    "location": data.get("location", "") or exp.get("location", ""),
                    "job_title": exp.get("title", ""),
                    "company_name": exp.get("company_name", ""),
                    "company_size": company_size,
                    "tenure_at_each_company": exp.get("tenure_at_each_company", ""),
                    "tenure_at_role": exp.get("tenure", ""),
                    "company_universal_name": exp.get("company_universal_name", ""),
                    "job_summary": exp.get("job_summary", ""),
                    "company_urn": exp.get("company_urn", ""),
                    "company_speciality": exp.get("company_speciality", []),
                    "total_tenure": data.get("total_tenure", ""),
                }
            )

        size_summary = generate_company_size_and_title_distribution(
            company_size_title_list
        )

        final_output_sizes_summaries.append(
            {"subcluster_key": entry["key"], "company_size_distribution": size_summary}
        )

        final_output.append(subcluster_data)

    # save_summary_stats(cluster_hierarchy_name_map, res)

    return final_output, final_output_sizes_summaries


def generate_company_size_and_title_distribution(company_size_title_list, top_k=3):
    """
    Input: List of tuples [(company_size, simplified_job_title), ...]
    Output: {
        "10 - 100": {
            "company_count": X,
            "most_frequent_words": [...],
            "most_frequent_bigrams": [...]
        },
        ...
    }
    """
    company_size_bins = [(10, 100), (101, 500), (501, 1000), (1001, float("inf"))]
    bin_labels = ["10 - 100", "101 - 500", "501 - 1000", "1000+"]

    # Initialize result structure
    size_distribution = {
        label: {
            "company_count": 0,
            "most_frequent_words": [],
            "most_frequent_bigrams": [],
        }
        for label in bin_labels
    }

    # Temporary buckets to collect simplified titles per bin
    titles_per_bin = {label: [] for label in bin_labels}

    for company_size, simplified_title in company_size_title_list:
        try:
            company_size = int(company_size)
        except (ValueError, TypeError):
            continue

        for (lower, upper), label in zip(company_size_bins, bin_labels):
            if lower <= company_size <= upper:
                size_distribution[label]["company_count"] += 1
                if simplified_title:
                    titles_per_bin[label].append(simplified_title.strip())
                break

    # Process top words and bigrams
    for label in bin_labels:
        summaries = summarize_words_and_bigrams(titles_per_bin[label], top_k=top_k)
        size_distribution[label]["most_frequent_words"] = summaries[
            "most_frequent_words"
        ]
        size_distribution[label]["most_frequent_bigrams"] = summaries[
            "most_frequent_bigrams"
        ]

    return size_distribution


def get_next_filename(base_name="messages", ext=".json"):
    pattern = re.compile(rf"^{base_name}_(\d+){re.escape(ext)}$")
    existing_files = [f for f in os.listdir() if pattern.match(f)]

    # Extract the numeric suffixes and find the next available index
    indices = [int(pattern.match(f).group(1)) for f in existing_files]
    next_index = max(indices) + 1 if indices else 0

    next_index = next_index % 5

    return f"{base_name}_{next_index}{ext}"


async def generate_cluster_details(user_prompt: str, retry_count=0):
    system_prompt = CLUSTERING_DETAILS_SYSTEM_PROMPT
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    res = await asynchronous_llm(
        model="gpt-4.1",
        fallback_openai_model="gpt-4o",
        messages=messages,
        temperature=0.2,
    )

    try:
        job_titles = extract_generic("<job_titles>", "</job_titles>", res)
        industry = extract_generic("<industries>", "</industries>", res)
        cluster_description = extract_generic("<description>", "</description>", res)
        niche_inference = extract_generic(
            "<niche_industry_inference>", "</niche_industry_inference>", res
        )

        job_titles = [title.strip() for title in job_titles.split(",") if title.strip()]
        industry = [ind.strip() for ind in industry.split(",") if ind.strip()]

        return {
            "job_titles": job_titles,
            "industry": industry,
            "cluster_description": cluster_description.strip(),
            "niche_industry_inference": niche_inference.strip(),
        }

    except Exception as e:
        logger.error("Parsing error:", e)
        if retry_count < 3:
            return await generate_cluster_details(user_prompt, retry_count + 1)
        else:
            return None


async def get_cluster_details(top_5_clusters):
    tasks = []
    top_5_profiles = []

    for cluster in top_5_clusters:
        cluster_profiles = []
        user_prompt = ""
        job_titles = []

        for idx, profile in enumerate(cluster):
            job_title = profile.get("job_title")
            if idx < 5:
                company_industry = profile.get("company_industry")
                company_description = profile.get("company_description")
                user_prompt += f"Profile Details: {idx + 1}\n"
                if company_industry:
                    user_prompt += (
                        f"<company_industry> {company_industry} </company_industry>\n"
                    )
                if company_description:
                    user_prompt += f"<company_description> {company_description} </company_description>\n"
                if job_title:
                    user_prompt += f"<job_title> {job_title} </job_title>\n"
                cluster_profiles.append(profile)
            else:
                if job_title:
                    job_titles.append(job_title)

        if job_titles:
            user_prompt += f"<some_other_job_titles_in_cluster> {', '.join(job_titles)} </some_other_job_titles_in_cluster>\n"

        tasks.append(asyncio.create_task(generate_cluster_details(user_prompt)))
        top_5_profiles.append(cluster_profiles)

    results = await asyncio.gather(*tasks)

    filtered_results = []
    filtered_profiles = []
    for result, profiles in zip(results, top_5_profiles):
        if (
            result
            and result["job_titles"] != ["NONE"]
            and result["industry"] != ["NONE"]
            and result["cluster_description"].strip().upper() != "NONE"
            and result["niche_industry_inference"].strip().upper() != "NONE"
        ):
            filtered_results.append(result)
            filtered_profiles.append(profiles)

    return filtered_results, filtered_profiles


async def get_network_clustering(
    public_identifiers: List[str], es_client: AsyncElasticsearch
):
    old_res = await get_profile_data(public_identifiers, es_client)
    res, top_5_clusters = await cluster_profiles(old_res)

    res, sizes_summary = await sub_cluster_profiles(res, top_5_clusters)

    cluster_details, top_5_profiles = await get_cluster_details(res)

    for cluster_detail, profiles in zip(cluster_details, res):
        profile_ids = [profile.get("profile_id") for profile in profiles]
        cluster_detail["profiles"] = profile_ids

    for cluster_detail, top_profiles in zip(cluster_details, top_5_profiles):
        cluster_detail["top_5_profiles"] = top_profiles

    for cluster_detail, size_summary in zip(cluster_details, sizes_summary):
        cluster_detail["company_size_title_stats"] = size_summary[
            "company_size_distribution"
        ]

    cluster_details = await merge_similar_clusters(cluster_details)

    return cluster_details


async def call_llm_for_cluster_merging(structured_input: str) -> str:
    """
    Calls the LLM with the pre-built structured input and the global prompt template.
    """

    prompt = CLUSTER_MERGE_PROMPT.format(structured_input=structured_input)

    try:
        response = await asynchronous_llm(
            messages=[{"role": "user", "content": prompt}],
            model="gpt-4.1-mini",
            temperature=0.2,
            max_tokens=2048,
            fallback_openai_model="gpt-4o",
            fallback_anthropic_model="claude-3-5-sonnet-latest",
            verbose=False,
        )
    except Exception as e:
        logger.error(f"Error calling asynchronous_llm: {e}")
        return {"error": True}

    return response


def select_distinct_top_profiles(profiles, max_count=5):
    seen_titles = set()
    selected_profiles = []
    unpicked_profiles = []

    def normalize_title(title):
        # Lowercase, remove non-word characters, collapse whitespace
        title = title.lower()
        title = re.sub(r"[^\w\s]", "", title)
        title = re.sub(r"\s+", " ", title)
        return title.strip()

    for profile in profiles:
        raw_title = profile.get("job_title", "")
        normalized = normalize_title(raw_title)

        if normalized not in seen_titles:
            seen_titles.add(normalized)
            selected_profiles.append(profile)
        else:
            unpicked_profiles.append(profile)

        if len(selected_profiles) >= max_count:
            break

    # If distinct profiles are fewer than max_count, randomly add remaining profiles
    if len(selected_profiles) < max_count:
        remaining_needed = max_count - len(selected_profiles)
        additional_profiles = random.sample(
            unpicked_profiles, min(remaining_needed, len(unpicked_profiles))
        )
        selected_profiles.extend(additional_profiles)
    return selected_profiles


# NEW MERGING SIZES


async def merge_similar_clusters(cluster_details):
    if len(cluster_details) <= 1:
        return cluster_details

    id_to_cluster = {}
    structured_input = ""

    for idx, cluster in enumerate(cluster_details, start=1):
        cluster_id = f"CLUSTER_{idx}"
        cluster["assigned_id"] = cluster_id

        stats_summary = ""
        if "company_size_title_stats" in cluster:
            stats_summary_text = generate_stats_textual_summary(
                cluster["company_size_title_stats"]
            )
            if stats_summary_text:
                stats_summary = f"\n{stats_summary_text}\n"

        structured_input += (
            f"<{cluster_id}>\n"
            f"Description: {cluster['cluster_description']}\n"
            f"Niche Industry Inference: {cluster.get('niche_industry_inference', 'N/A')}\n"
            f"Job Titles: {', '.join(cluster['job_titles'])}\n"
            f"Industries: {', '.join(cluster['industry'])}\n"
        )

        if stats_summary:
            structured_input += stats_summary

        structured_input += f"</{cluster_id}>\n\n"
        id_to_cluster[cluster_id] = cluster

    llm_response = await call_llm_for_cluster_merging(structured_input=structured_input)
    if not llm_response or (
        isinstance(llm_response, dict) and llm_response.get("error")
    ):
        return cluster_details

    try:
        merged_instructions = parse_llm_merge_response(llm_response)
    except Exception as e:
        logger.error(f"Error parsing LLM response: {e}")
        return cluster_details

    merged_clusters = []
    for merge in merged_instructions:
        merged_ids = merge["ids"]
        new_name = merge["new_name"]
        new_description = merge["new_description"]
        reason = merge["reason"]
        explanation = merge["explanation"]

        all_job_titles = set()
        all_industries = set()
        all_profiles = set()
        all_top_profiles = []
        niche_inferences = []

        local_merged_distribution = {}

        for cid in merged_ids:
            cluster = id_to_cluster.get(cid)
            if not cluster:
                continue

            all_job_titles.update(cluster["job_titles"])
            all_industries.update(cluster["industry"])
            all_profiles.update(cluster["profiles"])
            all_top_profiles.extend(cluster["top_5_profiles"])

            if "niche_industry_inference" in cluster:
                niche_inferences.append(cluster["niche_industry_inference"])

            company_size_summary = cluster.get("company_size_title_stats", {})
            for size_range, summary in company_size_summary.items():
                if size_range not in local_merged_distribution:
                    local_merged_distribution[size_range] = {
                        "company_count": 0,
                        "most_frequent_words": [],
                        "most_frequent_bigrams": [],
                    }

                local_merged_distribution[size_range]["company_count"] += summary.get(
                    "company_count", 0
                )

                existing_words = Counter(
                    dict(local_merged_distribution[size_range]["most_frequent_words"])
                )
                new_words = Counter(dict(summary.get("most_frequent_words", [])))
                merged_words = existing_words + new_words
                local_merged_distribution[size_range]["most_frequent_words"] = (
                    merged_words.most_common(5)
                )

                existing_bigrams = Counter(
                    dict(local_merged_distribution[size_range]["most_frequent_bigrams"])
                )
                new_bigrams = Counter(dict(summary.get("most_frequent_bigrams", [])))
                merged_bigrams = existing_bigrams + new_bigrams
                local_merged_distribution[size_range]["most_frequent_bigrams"] = (
                    merged_bigrams.most_common(5)
                )

        all_top_profiles = select_distinct_top_profiles(all_top_profiles)

        merged_cluster = {
            "cluster_name": new_name,
            "reason": reason,
            "explanation": explanation,
            "job_titles": list(all_job_titles),
            "industry": list(all_industries),
            "cluster_description": new_description,
            "niche_industry_inference": "; ".join(set(niche_inferences)),
            "profiles": list(all_profiles),
            "top_5_profiles": all_top_profiles,
            "company_size_title_stats": local_merged_distribution,
        }

        merged_clusters.append(merged_cluster)

    logger.info(
        "LLM-based cluster merging with new names and descriptions completed successfully"
    )
    return merged_clusters


def generate_stats_textual_summary(size_stats: dict) -> str:
    if not size_stats:
        return ""

    # Step 1: Remove bins with zero professionals
    filtered_bins = {
        size_bin: data
        for size_bin, data in size_stats.items()
        if data.get("company_count", 0) > 0
    }

    if not filtered_bins:
        return ""

    # Step 2: Determine significance threshold (20% of max)
    max_count = max(data["company_count"] for data in filtered_bins.values())
    threshold = max(1, int(0.2 * max_count))

    # Step 3: Keep only significant bins
    significant_bins = {
        size_bin: data
        for size_bin, data in filtered_bins.items()
        if data["company_count"] >= threshold
    }

    if not significant_bins:
        return ""

    # Step 4: Sort by company count descending
    sorted_bins = sorted(
        significant_bins.items(),
        key=lambda item: item[1]["company_count"],
        reverse=True,
    )

    # Step 5: Build structured block output
    lines = ["<COMPANY_SIZE_AND_ROLE_STATS>"]

    for size_bin, data in sorted_bins:
        count = data["company_count"]
        unigrams = data.get("most_frequent_words", [])[:3]
        bigrams = data.get("most_frequent_bigrams", [])[:3]

        lines.append(f'    <SCOMPANY_SIZE_BIN size="{size_bin}">')
        lines.append(f"      PROFILE_COUNT: {count}")
        lines.append(f"      TOP_UNIGRAMS_IN_JOB_TITLES: {unigrams}")
        lines.append(f"      TOP_BIGRAMS_IN_JOB_TITLES: {bigrams}")
        lines.append(f"    </COMPANY_SIZE_BIN>")

    lines.append("  </COMPANY_SIZE_AND_ROLE_STATS>")

    return "\n".join(lines)
