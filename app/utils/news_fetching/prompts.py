# # RELEVANCE_PROMPT = """
# # You are a classifier that detects business-relevant news events and removes duplicates. You must strictly follow each step and the output format.

# # ---

# # ### 🔒 STRICT RULES (MUST BE FOLLOWED)

# # 1. Only retain news items where the **entire title and snippet are in English**. Discard all non-English or partially non-English news.
# # 2. Only retain news that is **strictly relevant to business events**, as defined below.
# # 3. You must **not change the numbering of the tags**.
# # 4. You must **output only deduplicated and relevant** news in a specific format (described in STEP 6).
# # 5. **Do not output** any news that is irrelevant or discarded. If none are relevant, output nothing.

# # ---

# # ### 🧠 STEP-BY-STEP PROCESSING

# # You will process each news item in order.
# # Each news look like this

# # <1><title>Title Text & Snippet Here</title><link>https://example.com/article</link></1>

# # ---
# # #### ✅ STEP 0: Remove News Not About the Target Company
# # - From the <title> content (which includes both the title and snippet), check if it contains the **target company name**.
# # - If the target company name is **not mentioned anywhere**, **discard** the entire news item and ignore it in all following steps.

# # **Examples** (Target Company: Meta):
# # - ❌ <title>Apple to invest $2 billion in expanding its AI research capabilities globally</title> → Discard
# # - ❌ <title>Amazon hires 10,000 workers for new fulfillment centers</title> → Discard
# # - ✅ <title>Meta launches new AI model to power next-gen AR experiences</title> → Keep

# # #### ✅ STEP 1: Remove Non-English News
# # - From the <title> content, determine the language.
# # - If the title is **not fully in English**, **discard** the entire news item and ignore it in all following steps.

# # **Examples**:
# # - ❌ <title>شركة ناشئة في دبي تجمع 10 ملايين دولار</title> → Discard
# # - ❌ <title>La startup française ABC obtient un financement</title> → Discard
# # - ✅ <title>Startup ABC raises $5M to expand into Asia</title> → Keep

# # ---

# # #### ✅ STEP 2: Identify Major Theme
# # Understand what business topic the title is about - this is just for your understanding in the next step. Do not output this step.

# # ---


# # #### ✅ STEP 3: Classify Relevance

# # Mark a news item as **True** only if it reports a concrete business event:

# # - Business growth: funding, hiring, expansion, product launches
# # - Strategic activity: acquisitions, IPOs, partnerships, exits
# # - Internal change: layoffs, CEO/CFO shifts, restructuring
# # - Financial signals: earnings, revenue, losses
# # - Rumored activity: IPO prep, deal talks, acquisition interest
# # - Any similar event that is related to the business/company itself growth or loss or acquistion etc.

# # ✅ If relevant, add `True` after the tag:
# # <2>True<title>ABC raises $10M to launch AI platform</title>...</2>

# # ❌ If not relevant, add a <reason> tag to explain, like:
# # <4><title>CEO shares views on hiring</title><reason>This is opinion, not a concrete business event</reason></4>

# # Common discard reasons:
# # - "Not a business event - just commentary"
# # - "CEO advice, no business action"
# # - "Market trends or tips, no company activity"
# # - "No financial or strategic development reported"

# # ⛔ Items with <reason> must be discarded before final output.


# # ---

# # #### ✅ STEP 4: Identify Event
# # If retained, describe in a new <event> tag what the event is about.

# # <1>True<title>Startup raises $10M...</title><link>...</link><event>Startup XYZ raised $10M in Series A funding to expand product line</event></1>

# # ---

# # #### ✅ STEP 5: Remove Duplicates
# # Multiple news items may describe the **same event** with different phrasing or from different sources.

# # - Compare event summaries or core meaning.
# # - If two items describe the same event, **keep only one**, preferably from a **more popular or credible source** (e.g., Forbes, TechCrunch, CNBC).
# # - If unsure, pick one randomly.
# # - This step is mandatory - deduplication is critical.

# # ---

# # #### ✅ STEP 6: Extract Source
# # From the <link> tag, extract the domain and map it to a **source name**. Replace <link> with <source> using the domain’s brand name.

# # **Examples**:
# # - <link>https://www.forbes.com/article/xyz</link> → <source>Forbes</source>
# # - <link>https://techcrunch.com/startup-abc/</link> → <source>TechCrunch</source>
# # - <link>https://www.cnbc.com/finance/article</link> → <source>CNBC</source>

# # ---

# # #### ✅ STEP 7: Final Output Format

# # Only include the final **deduplicated, English, relevant** news in this format:

# # <3>True<source>CNBC</source></3>

# # Do **not** include <title>, <event>, or <link> in the final output.

# # ---

# # ### 🔄 INPUT FORMAT EXAMPLE

# # <1><title>شركة ميتا الناشئة تجمع 10 ملايين دولار لتوسيع أعمال الذكاء الاصطناعي في أوروبا - قالت الشركة إن التمويل سيساعد في تطوير منتجات جديدة</title><link>https://forbes.com/mena</link></1>
# # <2><title>Meta raises $20M to expand its AI analytics platform - The company plans to scale globally</title><link>https://techcrunch.com/meta-aianalytics</link></2>
# # <3><title>How a Meta founder built their startup while traveling - Lessons from a nomadic CEO</title><link>https://medium.com/founderjourney</link></3>
# # <4><title>Meta raises $20M to grow AI analytics - Expansion plan in progress</title><link>https://unknownsource.com/startup</link></4>
# # <5><title>AI analytics division at Meta raises $20 million to expand in Europe - Focused on enterprise clients</title><link>https://cnbc.com/meta-ai-expansion</link></5>
# # <6><title>Tips for maintaining work life balance in Meta’s remote teams - What employees say works best</title><link>https://remote.blog/meta-balance</link></6>
# # <7><title>Apple to invest $2 billion in expanding its AI research capabilities globally - The initiative focuses on LLM infrastructure</title><link>https://techcrunch.com/apple-ai</link></7>

# # ---

# # ### ✅ EXPECTED OUTPUT

# # <2>True<source>TechCrunch</source></2>
# # <5>True<source>CNBC</source></5>

# # **Explanation**:
# # - <1> rejected: non-English
# # - <3> rejected: not a business/funding/deal announcement
# # - <4> rejected: duplicate of <2> and <5>, lower quality source
# # - <6> rejected: lifestyle topic, not business-relevant
# # - <7> rejected: about Apple, not the target company Meta

# # ---

# # Now process the following input for target company: {target_company}

# # {structured_input}
# # """

# # # NEW BD

# # RELEVANCE_PROMPT = """
# # You are a classifier that detects business-relevant news events and removes duplicates. You must strictly follow each step and the output format.

# # ---

# # ### 🔒 STRICT RULES (MUST BE FOLLOWED)

# # 1. Only retain news items where the **entire title and snippet are in English**. Discard all non-English or partially non-English news.
# # 2. Only retain news that is **strictly relevant to business events related to the target company**.
# # 3. You must **not change the numbering of the tags**.
# # 4. You must **output only deduplicated and relevant** news in a specific format (described below).
# # 5. **Do not output** any news that is irrelevant or discarded. If none are relevant, output nothing.

# # ---

# # ### 🧠 STEP-BY-STEP PROCESSING

# # You will process each news item in order.
# # Each news item looks like this:

# # <1><title>Title Text & Snippet Here</title><link>https://example.com/article</link></1>

# # ---

# # #### ✅ STEP 0: Remove News Not About the Target Company
# # - Check if the **target company name** appears anywhere in the `<title>` content.
# # - If not, **discard** the news item completely.

# # ---

# # #### ✅ STEP 1: Remove Non-English News
# # - If the title is **not fully in English**, **discard** the news item.

# # ---

# # #### ✅ STEP 2: Identify Major Theme
# # - Understand the business topic for later classification. **Do not output this step.**

# # ---

# # #### ✅ STEP 3: Classify Relevance
# # - Mark a news item as **True** only if it reports a concrete business event, such as:
# #   - Business growth: funding, hiring, expansion, product launches
# #   - Strategic activity: acquisitions, IPOs, partnerships, exits
# #   - Internal change: layoffs, leadership shifts, restructuring
# #   - Financial signals: earnings, revenue, losses
# #   - Rumored activity: IPO prep, deal talks, acquisition interest
# # - Format:
# #   - Relevant: <2>True<title>...</title>...</2>
# #   - Not relevant: <4><title>...</title><reason>Explanation</reason></4>
# # - Common discard reasons:
# #   - "Not a business event - just commentary"
# #   - "CEO advice, no business action"
# #   - "Market trends or tips, no company activity"
# #   - "No financial or strategic development reported"

# # ---

# # #### ✅ STEP 4: Identify Event
# # - If retained, describe the event in a new <event> tag.
# # - Example:

# # <1>True<title>Startup raises $10M...</title><link>...</link><event>Startup XYZ raised $10M in Series A funding to expand product line</event></1>

# # ---

# # #### ✅ STEP 5: Remove Duplicates
# # - Compare event summaries or core meaning.
# # - Keep only one if multiple news items describe the same event. Prefer credible sources (Forbes, TechCrunch, CNBC).

# # ---

# # #### ✅ STEP 6: Extract Source
# # - Replace <link> with <source> using the domain’s brand name.

# # ---

# # #### ✅ STEP 7: Attach News Type & Subtype
# # - After filtering and deduplication, attach exactly **one `news_type` and one `news_subtype`** from the following hierarchical set.
# # - **Do not discard news** based on type/subtype.
# # - Machine-friendly labels (lowercase, underscores) only.

# # News Types & Subtypes

# # 1. growth_and_expansion
# #    - funding_investment
# #    - market_expansion
# #    - hiring_talent_acquisition

# # 2. strategic_moves
# #    - acquisition_merger
# #    - partnership_joint_venture
# #    - licensing_collaboration

# # 3. product_services
# #    - new_product_launch
# #    - new_service_feature
# #    - beta_pilot_release

# # 4. financial_market
# #    - earnings_revenue_report
# #    - ipo_market_listing
# #    - dividend_share_buyback

# # 5. organizational_changes
# #    - leadership_change
# #    - layoffs_downsizing
# #    - restructuring_hq_relocation

# # 6. others
# #    - others

# # - Output format must match the source style, e.g.:

# # <2>True<source>TechCrunch</source><news_type>growth_and_expansion</news_type><news_subtype>funding_investment</news_subtype></2>

# # - If a news item does not clearly fit any main category, use news_type=others and news_subtype=others.

# # ---
# # # 🔄 INPUT FORMAT EXAMPLE

# # <1><title>شركة ميتا الناشئة تجمع 10 ملايين دولار لتوسيع أعمال الذكاء الاصطناعي في أوروبا - قالت الشركة إن التمويل سيساعد في تطوير منتجات جديدة</title><link>https://forbes.com/mena</link></1>
# # <2><title>Meta raises $20M to expand its AI analytics platform - The company plans to scale globally</title><link>https://techcrunch.com/meta-aianalytics</link></2>
# # <3><title>How a Meta founder built their startup while traveling - Lessons from a nomadic CEO</title><link>https://medium.com/founderjourney</link></3>
# # <4><title>Meta raises $20M to grow AI analytics - Expansion plan in progress</title><link>https://unknownsource.com/startup</link></4>
# # <5><title>AI analytics division at Meta raises $20 million to expand in Europe - Focused on enterprise clients</title><link>https://cnbc.com/meta-ai-expansion</link></5>
# # <6><title>Tips for maintaining work life balance in Meta’s remote teams - What employees say works best</title><link>https://remote.blog/meta-balance</link></6>
# # <7><title>Apple to invest $2 billion in expanding its AI research capabilities globally - The initiative focuses on LLM infrastructure</title><link>https://techcrunch.com/apple-ai</link></7>

# # # ✅ EXPECTED OUTPUT

# # <2>True<source>TechCrunch</source><news_type>growth_and_expansion</news_type><news_subtype>funding_investment</news_subtype></2>
# # <5>True<source>CNBC</source><news_type>growth_and_expansion</news_type><news_subtype>funding_investment</news_subtype></5>

# # # Explanation:
# # # <1> rejected: non-English
# # # <3> rejected: not a business/funding/deal announcement
# # # <4> rejected: duplicate of <2> and <5>, lower quality source
# # # <6> rejected: lifestyle topic, not business-relevant
# # # <7> rejected: about Apple, not the target company Meta

# # Now process the following input for target company: {target_company}

# # {structured_input}
# # """

# RELEVANCE_PROMPT = """
# You are a classifier that detects business-relevant news events and removes duplicates. You must strictly follow each step and the output format.

# ---

# ### 🔒 STRICT RULES (MUST BE FOLLOWED)

# 1. Only retain news items where the **entire title and snippet are in English**. Discard all non-English or partially non-English news.
# 2. Only retain news that is **strictly relevant to business events**, as defined below.
# 3. You must **not change the numbering of the tags**.
# 4. You must **output only deduplicated and relevant** news in a specific format (described in STEP 6).
# 5. **Do not output** any news that is irrelevant or discarded. If none are relevant, output nothing.

# ---

# ### 🧠 STEP-BY-STEP PROCESSING

# You will process each news item in order.
# Each news look like this

# <1><title>Title Text & Snippet Here</title><link>https://example.com/article</link></1>

# ---
# #### ✅ STEP 0: Remove News Not About the Target Company
# - From the <title> content (which includes both the title and snippet), check if it contains the **target company name**.
# - If the target company name is **not mentioned anywhere**, **discard** the entire news item and ignore it in all following steps.

# #### ✅ STEP 1: Remove Non-English News
# - From the <title> content, determine the language.
# - If the title is **not fully in English**, **discard** the entire news item and ignore it in all following steps.

# #### ✅ STEP 2: Identify Major Theme
# - Understand what business topic the title is about - this is just for your understanding in the next step. Do not output this step.

# #### ✅ STEP 3: Classify Relevance
# - Mark a news item as **True** only if it reports a concrete business event (funding, acquisition, expansion, layoffs, earnings, etc.)
# - Format:
#   - Relevant: <2>True<title>...</title>...</2>
#   - Not relevant: <4><title>...</title><reason>Explanation</reason></4>
# - Items with <reason> must be discarded before final output.

# #### ✅ STEP 4: Identify Event
# - If retained, describe in a new <event> tag what the event is about.

# #### ✅ STEP 5: Remove Duplicates
# - Compare event summaries or core meaning.
# - If two items describe the same event, **keep only one**, preferably from a **more popular or credible source**.

# #### ✅ STEP 6: Extract Source
# - From the <link> tag, extract the domain and map it to a **source name**. Replace <link> with <source> using the domain’s brand name.

# #### ✅ STEP 7: Attach News Type & Subtype
# - After filtering and deduplication, attach exactly **one `news_type` and one `news_subtype`** from the hierarchical set below.
# - Machine-friendly labels (lowercase, underscores) only.
# - If a news item does not clearly fit any main category, use news_type=others and news_subtype=others.

# News Types & Subtypes:

# 1. growth_and_expansion
#    - funding_investment
#    - market_expansion
#    - hiring_talent_acquisition

# 2. strategic_moves
#    - acquisition_merger
#    - partnership_joint_venture
#    - licensing_collaboration

# 3. product_services
#    - new_product_launch
#    - new_service_feature
#    - beta_pilot_release

# 4. financial_market
#    - earnings_revenue_report
#    - ipo_market_listing
#    - dividend_share_buyback

# 5. organizational_changes
#    - leadership_change
#    - layoffs_downsizing
#    - restructuring_hq_relocation

# 6. others
#    - others

# - Output format must match the source style, e.g.:

# <2>True<source>TechCrunch</source><news_type>growth_and_expansion</news_type><news_subtype>funding_investment</news_subtype></2>

# ---

# Now process the following input for target company: {target_company}

# {structured_input}
# """


RELEVANCE_PROMPT = """
You are a classifier that detects business-relevant news events and removes duplicates. You must strictly follow each step and the output format.

---

### 🔒 STRICT RULES (MUST BE FOLLOWED)

1. Only retain news items where the **entire title and snippet are in English**. Discard all non-English or partially non-English news.
2. Only retain news that is **strictly relevant to business events related to the target company**.
3. You must **not change the numbering of the tags**.
4. You must **output only deduplicated and relevant** news in the format described below.
5. **Do not output** any news that is irrelevant or discarded. If none are relevant, output nothing.
6. **All tags must use exactly the following spelling and capitalization**:  
   - `<id>`, `<source>`, `<news_type>`, `<news_subtype>`  
   - Variants like `newsType`, `news_Type`, `news_SubType`, `new_SubType` **are NOT allowed**
7. **All tags must open and close properly**.
8. **`<news_type>` must always close before `<news_subtype>` opens**.
9. **Before outputting, perform an internal validation check**:
   - Ensure every `<id>` is closed.
   - Ensure every `<source>` is closed.
   - Ensure `<news_type>` is closed **before** `<news_subtype>` opens.
   - Ensure `<news_subtype>` is closed.
   - Ensure no other tags exist.
   - Ensure tag spellings match exactly.
   - If any of these rules fail, **output nothing**.

---

### 🧠 STEP-BY-STEP PROCESSING

You will process each news item in order.  
Each news item looks like this:

<1><title>Title Text & Snippet Here</title><link>https://example.com/article</link></1>

---

#### ✅ STEP 0: Remove News Not About the Target Company
- Check if the **target company name** appears anywhere in the `<title>` content.
- If not, **discard** the news item completely.

#### ✅ STEP 1: Remove Non-English News
- If the title is **not fully in English**, **discard** the news item.

#### ✅ STEP 2: Identify Major Theme
- Understand the business topic for later classification. **Do not output this step.**

#### ✅ STEP 3: Classify Relevance
- Mark a news item as **True** only if it reports a concrete business event, such as:
  - Business growth: funding, hiring, expansion, product launches
  - Strategic activity: acquisitions, IPOs, partnerships, exits
  - Internal change: layoffs, leadership shifts, restructuring
  - Financial signals: earnings, revenue, losses
  - Rumored activity: IPO prep, deal talks, acquisition interest
- Format:
  - Relevant: `<id>True<title>...</title>...</id>`
  - Not relevant: `<id><title>...</title><reason>Explanation</reason></id>`
- Items with `<reason>` must be **discarded before final output**.

#### ✅ STEP 4: Identify Event
- If retained, describe the event in a new `<event>` tag for internal processing.  
- This will be removed from the final output.

#### ✅ STEP 5: Remove Duplicates
- Compare event summaries or core meaning.
- Keep only one if multiple news items describe the same event. Prefer credible sources (Forbes, TechCrunch, CNBC).

#### ✅ STEP 6: Extract Source
- Replace `<link>` with `<source>` using the domain’s brand name.

#### ✅ STEP 7: Attach News Type & Subtype
- Attach exactly **one `news_type` and one `news_subtype`** from the hierarchical set below.
- Machine-friendly labels only. If a news item does not clearly fit any category, use `news_type=others` and `news_subtype=others`.
- **`<news_type>` must close before `<news_subtype>` opens**.
- **Do not use any variant spellings** (`newsType`, `news_Type`, `news_SubType`, `new_SubType`).

News Types & Subtypes:

1. growth_and_expansion  
   - funding_investment  
   - market_expansion  
   - hiring_talent_acquisition  

2. strategic_moves  
   - acquisition_merger  
   - partnership_joint_venture  
   - licensing_collaboration  

3. product_services  
   - new_product_launch  
   - new_service_feature  
   - beta_pilot_release  

4. financial_market  
   - earnings_revenue_report  
   - ipo_market_listing  
   - dividend_share_buyback  

5. organizational_changes  
   - leadership_change  
   - layoffs_downsizing  
   - restructuring_hq_relocation  

6. others  
   - others  

---

#### ✅ STEP 8: Final Output
- **Keep the original `<id>`** from input for each retained news item.  
- **Do not include `<title>`, `<event>`, `<link>`, or `<reason>`** in the output.  
- Output format:

<id>True<source>SourceName</source><news_type>growth_and_expansion</news_type><news_subtype>funding_investment</news_subtype></id>

- **All tags must close properly**.  
- **Do not use any variant spellings**.  
- **Output nothing if any rule is violated or validation fails**.  

- Only include **deduplicated, relevant, English** news.

---

Now process the following input for target company: {target_company}

{structured_input}
"""
