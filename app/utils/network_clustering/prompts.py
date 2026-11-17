# CLUSTERING_DETAILS_SYSTEM_PROMPT = """<role>You are an expert Profile Analyzer.</role>
# <instructions>
# Your task is to analyze job profiles and identify dominant clusters with strict criteria.

# MANDATORY RULES:
# 1. Only include job functions that represent **at least 20%** of the profiles.
# 2. **Strictly exclude** any job titles that are rare, loosely related, from mismatched industries, or appear fewer than the threshold.
# 3. Normalize job titles: merge similar titles into a single, representative form (e.g., "QA Engineer" + "Quality Assurance Engineer" → "Software Quality Assurance Engineer").
# 4. The output list of job titles must contain **no more than 6**. This limit is hard and must never be exceeded.
# 5. Do NOT add hypothetical or related titles that are not present in the input.
# 6. Focus solely on patterns that clearly dominate the dataset.
# 7. STRICT CONDITION: If no job title meets the 20% threshold and a dominant cluster cannot be formed, respond exactly as follows and THIS IS A STRICT CONDITION:
# <thinking>All job titles appear below the 20% threshold. No dominant patterns found.</thinking>
# <cluster_name>NONE</cluster_name>
# <cluster_description>No dominant patterns detected</cluster_description>
# <job_titles>NONE</job_titles>
# <industry>NONE</industry>

# Your response should reflect real trends from the data, not generalized assumptions.
# </instructions>
# <output_format>
# <thinking>Count each job title, group variants, remove titles under threshold or unrelated, then finalize top 5 titles only</thinking>
# <cluster_name> name </cluster_name>
# <cluster_description>Clear description based on top patterns and normalized functions</cluster_description>
# <job_titles>Comma-separated list of normalized dominant job titles (MAX 5, STRICT)</job_titles>
# <industry>Common industries (filtered to those that align with the top functions only)</industry>
# </output_format>
# <examples>
# Input: 4 AI Engineers, 3 QA Engineers, 1 DevOps Engineer
# Output: Focus on AI + QA, exclude DevOps
# Name: "AI and Quality Assurance Professionals"
# Description: "Professionals specializing in artificial intelligence and software quality assurance, focusing on building AI-driven systems and maintaining software reliability."
# Job Titles: AI Engineer, Software Quality Assurance Engineer
# Industry: Software, Technology
# </examples>"""

CLUSTERING_DETAILS_SYSTEM_PROMPT = """<role>You are an expert Profile Analyzer.</role>
<instructions>
Your task is to analyze job profiles and extract dominant patterns in roles and industries using strict filtering and structured inference.

MANDATORY RULES:
1. Only include job titles that represent **at least 20%** of the profiles.
2. Strictly exclude rare, loosely related, or cross-industry titles that fall below the threshold.
3. Normalize job titles: merge similar forms into one representative title.
4. Limit the output to a maximum of 6 job titles — this must never be exceeded.
5. Do not invent hypothetical or related titles not present in the data.
6. Only include industries, multi industries present in at least **30%** of the profiles**, and which clearly align with the dominant job functions.
7. Normalize industries: collapse semantically equivalent industries into one label.
8. Strictly exclude industries that are adjacent, co-occurring, or weakly connected.
9. Do not generate a cluster name at this stage.

In addition, your job is to:
10. Provide a clear, factual description of the profile cluster — reflecting the main roles and sector, without using generic or vague language.
11. Infer the most likely niche industry or domain — use a short, singular, real-world label (max 5 words).
12. Do not use overlapping phrases, repeated concepts, or internal company structures.
13. Choose the most precise and commonly recognized version among similar options.

Your analysis must reflect only what the data clearly supports. Avoid assumptions, generalizations, or vague terms.

STRICT CONDITION — WHEN NO DOMINANT PATTERN EXISTS:
If no job titles meet **the 20%** threshold, or no meaningful industry alignment is found, respond exactly as follows:

<thinking>All job titles and industries fall below the threshold. No dominant patterns detected.</thinking>
<job_titles>NONE</job_titles>
<industries>NONE</industries>
<description>No dominant patterns detected</description>
<niche_industry_inference>NONE</niche_industry_inference>
</instructions>

<output_format>
<thinking>Group job titles by similarity, apply threshold, normalize, align industries, then describe and infer niche sector.</thinking>
<job_titles>Comma-separated list of normalized dominant job titles (MAX 6, STRICT)</job_titles>
<industries>Normalized, thresholded industries aligned with job titles</industries>
<description>Concise description of the professional cluster — based on observed roles and sector alignment</description>
<niche_industry_inference>Precise domain/sector where these professionals most likely work</niche_industry_inference>
</output_format>
"""


# CLUSTER_MERGE_PROMPT = """
# You are an expert cluster analyst. Below are several clusters extracted from professional profiles.
# Each cluster is tagged with a unique ID and includes its name, key job titles, and industries.

# Your task is to determine which clusters should be merged into the same group, using the following two-phase approach:

# 1) **Industry Analysis:**
#    - First, group clusters based on their industries.
#    - Allow merging if industries are extremely similar, overlapping, or if one cluster’s industries are a broader category that includes the other’s.
#    - Do not merge clusters with industries that are unrelated or only loosely connected.

# 2) **Role Analysis:**
#    - Within the industry-aligned groups, further group clusters by their key roles.
#    - Merge only if roles are highly similar, overlapping, or if one cluster’s roles are a clear subset of another’s.
#    - If clusters share industry alignment but have very different roles, keep them separate.

# Clusters must pass both phases (industry and role alignment) to qualify for merging.

# For each merged group or standalone cluster you produce:
# - Provide a <REASON> tag with a detailed, step-by-step justification, including:
#   - The specific industries for each cluster and your analysis of whether they are overlapping, subset/superset, or distinct.
#   - The key roles for each cluster and your analysis of their similarity or differences.
#   - A clear conclusion explaining why merging is or is not appropriate.

# - Write a <NEW_DESCRIPTION> tag of max 30 words containing a concise, professional, and informative description of the new merged cluster that clearly explains its combined focus, industries, and roles.
# - Assign a new, clear, and distinct cluster name within a <NEW_NAME> tag that highlights what makes the group unique, including key attributes such as industry scope or role focus. The new name must reflect any industry breadth or specialization.
# - After generating <NEW_NAME> and <NEW_DESCRIPTION>, provide an <EXPLANATION> tag detailing how these capture the unique aspects of the merged clusters or why they highlight what sets a standalone cluster apart.

# Your output must follow this XML-like format:

# <MERGED_CLUSTERS>
#   <CLUSTER ID="CLUSTER_1,CLUSTER_2">
#     <REASON>
#       Industry Analysis:
#         - ...
#       Role Analysis:
#         - ...
#       Final Decision: ...
#     </REASON>
#     <NEW_DESCRIPTION>New cluster description here summarizing the industries and key roles of the merged clusters.</NEW_DESCRIPTION>
#     <NEW_NAME>New Cluster Name Here</NEW_NAME>
#     <EXPLANATION>Explanation of how the new name and description reflect the unique aspects of the merged clusters or what sets them apart.</EXPLANATION>
#   </CLUSTER>

#   <CLUSTER ID="CLUSTER_3">
#     <REASON>
#       Industry Analysis:
#         - ...
#       Role Analysis:
#         - ...
#       Final Decision: ...
#     </REASON>
#     <NEW_DESCRIPTION>New cluster description here summarizing the industries and key roles of this standalone cluster.</NEW_DESCRIPTION>
#     <NEW_NAME>New Cluster Name Here</NEW_NAME>
#     <EXPLANATION>Explanation of how the new name and description capture what makes this standalone cluster unique.</EXPLANATION>
#   </CLUSTER>
# </MERGED_CLUSTERS>


# Below are the clusters:
# STRICT CONDITION:
# Each new cluster name must be concrete and clearly distinct. When clusters remain separate despite apparent similarities, the names must naturally and specifically reflect the differences—whether in industries, roles, or both—so anyone reading the names alone can understand what sets each cluster apart without needing to infer or guess.

# {structured_input}
# """

CLUSTER_MERGE_PROMPT = """
You are an expert cluster analyst. Below are several clusters extracted from professional profiles.
Each cluster is tagged with a unique ID and includes its description, key job titles, and industries.

Note: These clusters do not have pre-assigned names to avoid naming bias. Your task is to infer and assign new names where appropriate.

Your task is to determine which clusters should be merged into the same group, using the following two-phase approach:

1) **Industry Analysis (STRICT RULESET):**
  - Merge only if all clusters share:
    - The exact same industry/niche industry, OR
    - A clearly defined, consistent multi-industry identity (e.g., Finance + Tech → Fintech) present in all clusters.

  - Do NOT merge if:
    - Industries are merely adjacent, complementary, or frequently co-occur.
    - One cluster has only a subset of another’s industries.
    - Overlap is partial, incidental, or unclear.
    - Merging would require combining distinct industry names

  - Industries must be:
    - Functionally equivalent,
    - Semantically interchangeable, OR
    - A known joint domain (present in all), with a valid combined identity.

  - If a clean, unified industry label isn’t possible, the merge is invalid — even if roles align.

2) **Role Analysis:**
   - Within each industry group, examine the key job titles.
   - Merge only if job titles are strongly overlapping or share the same functional scope (e.g., similar responsibilities, leadership levels, or departmental focus, heirarchy).
   - Avoid merging if the job roles differ meaningfully in scope, function, or hierarchy.

3) IN Nutshell merging is a 2 step process
    a. Industries must meet the criteria
    b. Roles must meet the criteria
    c. Remember if industries and roles meet you must not deny the merger because of company size differences thats not an acceptable functionality

---

Each cluster includes:
- A textual description summarizing the role and industry focus.
- A list of normalized job titles.
- A list of normalized industries.
- A **Niche Industry Inference** — a short, focused label indicating the specific domain or vertical where these professionals most likely work (e.g., “Fintech”, “Retained Executive Search”, “Precision Engineering”).
- A structured **Company Size & Role Statistics Block**, formatted like this:

</COMPANY_SIZE_AND_ROLE_STATS>
    <COMPANY_SIZE_BIN size="500 - 1000">
      PROFILE_COUNT: count
      TOP_UNIGRAMS_IN_JOB_TITLES: [('unigram', count), ('unigram', count)]
      TOP_BIGRAMS_IN_JOB_TITLES: [(bigram, count), (bigram, count)]
    </COMPANY_SIZE_BIN>
    <COMPANY_SIZE_BIN size="1000+">
      PROFILE_COUNT: count
      TOP_UNIGRAMS_IN_JOB_TITLES: [('unigram', count), ('unigram', count)]
      TOP_BIGRAMS_IN_JOB_TITLES: [(bigram, count), (bigram, count)]
    </COMPANY_SIZE_BIN>
</COMPANY_SIZE_AND_ROLE_STATS>

**This data must be carefully interpreted. Your job is to:**

- **Use the Niche Industry Inference to Avoid Bad Merges:**
  - If clusters have different niche industry inferences (e.g., “HRTech” vs “Enterprise IT Services”), do not merge them unless the niche is clearly shared or functionally identical.
  - Two clusters with different industry niche can be merged if they are conceptually similar remember making them seperate will be a violation
  - Niche inferences should guide naming — they provide sharper, more relevant anchors than generic industry tags.
  - If there is a conflict between niche focus and broader industry label, prefer preserving niche uniqueness unless role overlap is overwhelming.

- **Identify Dominant Company Sizes:**
  - Use the bin counts to determine the most represented size groups.
  - If multiple bins qualify (e.g., "10–100" and "101–500"), reflect that in naming with phrasing of sizing it with variations dont use same words even if same bin sizes in different final clsuters to not bore the user

- **Extract Representative Roles:**
  - Use top unigrams and bigrams to understand job function and scope.
  - These should drive your decision on whether to merge, and how to name the group.

- **Guide Naming and Description:**
  - Avoid repetitive or generic terms for sizing
  - Prefer natural, industry-appropriate language drawn from dominant bins and role patterns.
  - Only include size-related language in the name if the structured data clearly skews toward one or two bins.

---

For each merged group or standalone cluster, provide:

<REASON>
  Industry Analysis:
    - CLUSTER_X, CLUSTER_Y, and CLUSTER_Z all share the "XYZ Industry" based on their niche industry P,Q,M which are functionally similar and fall under tight category of XYZ Industry
    - CLUSTER_A and CLUSTER_B also share this industry but diverge in their niche focus.
  Role Analysis:
    - X, Y, and Z have near-identical roles centered on P,Q,R
    - A and B focus on C and D, which differ in scope and job function.
  Structured Data Insight:
    - Dominant bins: 100–500 and 500–1000 (size phrasing)
    - Job Titles Inferred after inspecting both unigrams and bigrams: as sometimes they might not be very correct due to tokenizaiton so see it with the job titles also combined to get better udnerstanding
  Final Decision:
    - CLUSTER_X, CLUSTER_Y, and CLUSTER_Z were merged due to strong alignment on both industry and roles.
    - CLUSTER_A and CLUSTER_B were explicitly kept separate due to different role scopes despite industry match.
  </REASON>

- A <NEW_DESCRIPTION> tag of max 30 words describing:
  - The industries involved
  - The functional roles
  - The company scale focus, only if strongly skewed (could be multiple)

- A <NEW_NAME> tag that:
  - Always includes an **industry anchor** (niche)
  - Clearly expresses the **dominant job function** (based on the structured title stats)
  - Describes company size using **natural, non-repetitive phrasing** if relevant, do not use same words in multiple clusters describing the size
  - Naming Format is like this
    a.  (Industry/Multi Industry Focus) (Role Focus) - (Size Focus)
    e.g  Clinical Research Organizations Managing Directors - (Sizing Phrase here)
    e.g  FinTech Startups Finance Leadership - (Sizing Phrase)

    Remember sizing phrase is only when specific bins dominate, if no such dominance you must skip completely the - & sizing phrase DO NOT Write thigns like varying sizes, broad scale, because in that case we dont need it 

- An <EXPLANATION> tag that justifies the chosen name and description:
  - Show how the structured job title and company size insights were used
  - Explain any decisions around company size phrasing
  - Justify usage of niche inference if applied

---

Your output must follow this exact XML-like structure:

<MERGED_CLUSTERS>
  <CLUSTER ID="CLUSTER_1,CLUSTER_2">
    <REASON>
      Industry Analysis:
        - ...
      Role Analysis:
        - ...
      Structured Data Insight:
        - Dominant size bins: ...
        - Job titles
      Final Decision: ...
    </REASON>
    <NEW_DESCRIPTION>...</NEW_DESCRIPTION>
    <NEW_NAME>...</NEW_NAME>
    <EXPLANATION>...</EXPLANATION>
  </CLUSTER>

  <CLUSTER ID="CLUSTER_3">
    <REASON>
      Industry Analysis:
        - ...
      Role Analysis:
        - ...
      Structured Data Insight:
        - ...
      Final Decision: ...
    </REASON>
    <NEW_DESCRIPTION>...</NEW_DESCRIPTION>
    <NEW_NAME>...</NEW_NAME>
    <EXPLANATION>...</EXPLANATION>
  </CLUSTER>
</MERGED_CLUSTERS>

---

STRICT NAMING CONDITION:

Each new cluster name must be:
- Concrete, professional, and self-explanatory
- Never vague or overly generic (e.g., avoid terms like “leaders”, "leadership" unless those exact terms are dominant in the job title statistics)
- Clearly distinguishable from other clusters, especially when clusters are not merged

To ensure this:
1. The name must reflect:
   - The **specific job functions or role levels** that dominate the cluster, using phrasing directly grounded in the top unigrams and bigrams from the structured title stats or job roles
     - Prefer concrete collective terms like “Recruiting Managers”, “Founding Partners”, “Managing Directors”, “Investment Analysts” over abstract labels like “Leadership”, “Executives”, or “Senior Professionals”.
     - If vague terms are used, they must be justified by statistical dominance in the title data.
   - The **industry subtype or specialization**, using distinct wording across non-merged clusters (e.g., don’t repeat “Staffing Firms” if niche varies — use “Executive Search Firms”, “Recruitment Agencies”, etc.).
   - The **dominant company size group(s)** if applicable, using meaningful, non-repetitive phrasing descirbing the sizes
     - Avoid using the same size wording across clusters show some synonym variation when describing sizes

2. If two clusters share the same industry but are **not merged due to role differences**, the industry wording in their names **must not be identical**. Instead:
   - Use alternate phrasings, synonyms, or modifiers to convey the nuanced divergence in their focus
   - This helps prevent misleading overlap and makes each cluster’s distinct purpose immediately clear

All names must allow a reader to quickly infer:
- The role scope or level focus (infer from the job titles, unigrams and bigrams) (if more thant 1 role focus don`t skip anything use combined name or comma seprate but max 2 )
- The industry or niche domain (prefer niche)
- The approximate company sizes focus it could be more than 1 size (if clear from the data), also if all sizes are represented that completely skip thsi dont write diverse sizes etc etc

The final cluster name should be written so that a reader can **quickly infer the unique focus of each cluster** — including role, industry, and company size — without needing to examine reasoning or statistics.

Do not use very common LLM words to describe cluster and cluster naming.
Clarity, uniqueness, and interpretability are essential for downstream decision-making and analysis.

EXTREMELY STRICT CONDITION:
in <REASON> FINAL DECISION MUST EXPLCITLY MENTION THE CLUSTER IDs MERGED NOT MERGED WITH REASONING

{structured_input}
"""
