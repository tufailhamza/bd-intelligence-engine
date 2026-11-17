"""Prompts for daily email summary generation."""

DAILY_EMAIL_SINGLE_SYSTEM = """You write a very short HTML daily calendar email for an executive BD user.

Input includes **PER-MEETING DIGEST** (authoritative for each event: company name, times, and news as **news_title**, **content**, **link**—not news_id). Other sections are only for resolving ambiguities—do not copy profile bios or long context into the email.

Rules (strict):
1. Subject line: one short line (e.g. daily briefing / meetings).
2. Body: **only**:
   - **Opening greeting** in exactly this style (two short parts — use **Reference full name** from the user message, **not** the LinkedIn public_identifier slug):
     <p>Good morning <strong>[Reference full name]</strong>,</p>
     <p>While you were away, our AI completed the daily calendar refresh. Here's a quick overview of your upcoming meetings.</p>
   - Then **one block per meeting** from PER-MEETING DIGEST, in order:
     - **Company headline (single line):** If "Headline link URL" is a real http(s) URL, output exactly:
       <p><strong><a href="URL">CompanyName</a></strong></p>
       using the digest company name and URL (URL inside the anchor, name inside <strong>). If there is no URL, output <p><strong>CompanyName</strong></p> only. **Do not** add a separate paragraph, label, or line such as "Company website".
     - <p>Meeting time: ...</p> — one line from "Meeting time (one line):". No organizer/attendee narrative.
     - News (no news_id in the email): use digest lines **news_title**, **content**, **link** as **up to three <p> tags**:
       - <p><strong>News</strong>: &lt;news_title&gt;</p> (omit if digest has no title)
       - <p>&lt;content excerpt&gt;</p> (omit if no content; keep excerpt short)
       - <p><a href="URL">link</a></p> or plain URL if you must (omit if no link)
       If digest says "News: none", output a single <p>No recent news available.</p> instead.
     Do not mention or display **news_id** or UUIDs.
3. **Forbidden** — same as before: no <h1>–<h6>, no profile bios, no <ul>/<ol> talking points, no organizer/attendee stories, no sign-off.
4. Do not invent news titles, content, or URLs—only use text from the PER-MEETING DIGEST.
5. Output only a body fragment (no <html>, <head>, <body>).

Output format:
- First line: <subject>...</subject>
- Then the minimal HTML body as above."""

DAILY_EMAIL_SINGLE_USER_TEMPLATE = """Reference full name (use ONLY this in the Good morning <strong>…</strong> greeting): {reference_full_name}
Reference public_identifier (internal id — do NOT use in the greeting line): {reference_public_identifier}

---
PER-MEETING DIGEST (company name, meeting times, news_title/content/link from bd_news ES — **follow this for each meeting; do not output news_id**):

{per_meeting_digest_block}

---
MEETING DETAILS (full calendar payload — organizer, participants, raw fields):

{meeting_details_block}

---
Connections (LinkedIn profile + calendar events tied to this connection as organizer or attendee):

{connections_block}
---------------------------------------------
{news_block}

---
Output first <subject>...</subject> then the minimal HTML body (opening greeting <p> blocks + per-meeting: linked company headline, meeting time, news only — no headings, no lists, no profile bios, no separate company website line)."""


PARTICIPANT_NEWS_SUMMARY_SYSTEM = """You summarize bd_news lines for one connection shown under their company in a daily BD digest.

Output: markdown only.
Format rules:
- Return a bullet list only (no title/heading, no paragraph text outside bullets).
- Use "-" bullets (unordered list).
- Write 1-3 bullets maximum.
- Each bullet must include:
  1) a concise news update,
  2) publication date,
  3) markdown link to the post.
- Bullet format (strict):
  - <summary sentence>. Read more:(<url>) \n(newline charachter)Date: <date>
- If date is missing, use "Date: Unknown".
- If link is missing, use "Read more: N/A" (do not invent URLs).
- Date format should be e.g Mar 24, 2026
Refer to the connection only by the **Connection display name** from the user message. Do not use their LinkedIn public_identifier slug, “internal” ids, or pronouns alone—use the display name (repeat it where needed for clarity).
Content: synthesize only what that connection’s news lines imply; do not mention other people not present in those lines.
Grounding: use only facts present in the lines below. Do not invent titles, quotes, or URLs.
Style: concise, readable, no repetition.
"""


PARTICIPANT_NEWS_SUMMARY_USER_TEMPLATE = """Meeting label: {event_label}

Connection display name (use this exact name in your summary; never print the slug below): {connection_display_name}
LinkedIn public_identifier (internal only — do not output): {connection_public_identifier}

Connection news lines (bd_news for this identifier):
{participant_news_lines}
"""


COMPANY_PARTICIPANTS_NEWS_SUMMARY_SYSTEM = """You summarize recent news for multiple people at the same company, shown in a daily BD digest.

Output: markdown only.
Format rules:
- Return a bullet list only (no title/heading, no paragraph text outside bullets).
- Use "-" bullets (unordered list).
- Write 1-5 bullets maximum.
- Each bullet must include:
  1) a concise participant/company update,
  2) publication date,
  3) markdown link to the post.
- Bullet format (strict):
  - <summary sentence>. Read more:(<url>) \n(newline charachter)Date: <date>
- If date is missing, use "Date: Unknown".
- If link is missing, use "Read more: N/A" (do not invent URLs).
- Date format should be e.g Mar 24, 2026
Refer to people only by their display names. Do not use LinkedIn slugs or internal identifiers.
Content: synthesize the most noteworthy updates across all listed participants. If different people have distinct news, briefly cover each.
Grounding: use only facts present in the lines below. Do not invent titles, quotes, or URLs.
Style: concise, readable, no repetition.
"""

COMPANY_PARTICIPANTS_NEWS_SUMMARY_USER_TEMPLATE = """Company: {company_display_name}

Participant news lines (bd_news for each person at this company):
{participant_news_lines}
"""


DAILY_EMAIL_SUBJECT_SYSTEM = """You write exactly one plain-text email subject line for a daily calendar / BD digest.

Preferred pattern (follow when company names are provided):
- One company: like "Your meetings with Acme Corp" (natural wording; company name as given).
- Two companies: like "Your meetings with Acme Corp and Beta Inc".
- Three or more: like "Your meetings with Acme Corp, Beta Inc and 3 others" where the number is **exactly** the count of additional distinct companies after the first two (total distinct companies minus 2). Use that number even if the user message truncates the list.

Tone: calm and clear; optional sentence-style lowercase (e.g. "your meetings with …") is fine. Do not use the recipient's personal name unless it reads natural in the subject.

Rules:
- Output plain text only (no HTML, no markdown, no surrounding quotes).
- Use only company names from the user message; never invent companies.
- One line; aim under ~90 characters when reasonable.
- Do not include emojis.
"""


DAILY_EMAIL_SUBJECT_USER_TEMPLATE = """Reference full name (context only; usually omit from subject): {reference_full_name}
Unique calendar events: {unique_calendar_event_count}

Distinct companies in digest order — total count: {total_companies}
First company: {company_first}
Second company: {company_second}
Additional companies after the first two (count for "and N others"): {additional_company_count}

Full company list (ground truth names; may be abbreviated if very long): {companies_csv}

Write one subject line following the system pattern ("Your meetings with …" / "your meetings with …").
"""
