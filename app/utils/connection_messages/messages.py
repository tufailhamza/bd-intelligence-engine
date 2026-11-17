import asyncio
import html
import logging
import re
from typing import Dict

from qutils.llm.asynchronous import invoke

from app.utils.connection_messages.prompts import (
    LINKEDIN_MESSAGE_CHANNEL_HINT,
    POST_LINK_HTML_INLINE,
    PREDICTIVE_EMAIL_MESSAGE_PROMPT_TEMPLATE,
    PREDICTIVE_LINKEDIN_MESSAGE_PROMPT_TEMPLATE,
    PREDICTIVE_STAKEHOLDER_MESSAGE_HINT,
    PREDICTIVE_TRIGGER_MESSAGE_HINT,
    linkedin_message_prompt_suggested_message,
    linkedin_message_prompt_catch_up,
    linkedin_message_prompt_offer_support,
    linkedin_message_prompt_congratulate,
    linkedin_subject_line_prompt,
    email_prompt_catch_up,
    email_prompt_congratulate,
    email_prompt_offer_support,
    email_prompt_suggested_message,
)

logger = logging.getLogger(__name__)


def extract_generic(start: str, end: str, text: str):
    match = re.search(rf"{start}(.*?){end}", text, re.DOTALL)
    return match.group(1) if match else None


def _resolve_payload_link(payload) -> str:
    if not isinstance(payload, dict):
        return ""
    raw = payload.get("link")
    if isinstance(raw, str):
        return raw.strip()
    if raw is not None:
        return str(raw).strip()
    return ""


def _body_has_anchor_for_url(body: str, url: str) -> bool:
    if not url or not body:
        return True
    pat = re.compile(
        r"<a\s[^>]*?\bhref\s*=\s*(['\"])" + re.escape(url) + r"\1",
        re.IGNORECASE | re.DOTALL,
    )
    if pat.search(body):
        return True
    alt = url.replace("&", "&amp;")
    if alt != url:
        pat2 = re.compile(
            r"<a\s[^>]*?\bhref\s*=\s*(['\"])" + re.escape(alt) + r"\1",
            re.IGNORECASE | re.DOTALL,
        )
        if pat2.search(body):
            return True
    return False


def _ensure_post_link_fallback_body(body: str, url: str) -> str:
    """
    If payload included a post link but the model omitted <a href>, inject one inline:
    1) wrap a common referring phrase; 2) else replace bare URL; 3) else insert before sign-off/end.
    """
    if not url or not (body or "").strip():
        return body
    if _body_has_anchor_for_url(body, url):
        return body

    esc_href = html.escape(url, quote=True)

    # Prefer wrapping natural phrases that refer to the post (preserve matched casing).
    phrase_patterns = [
        r"\byour repost\b",
        r"\byour post\b",
        r"\bthat repost\b",
        r"\bthat post\b",
        r"\bthis post\b",
        r"\bthe post\b",
        r"\bwhat you reshared\b",
        r"\bwhat you shared\b",
    ]
    for pat in phrase_patterns:
        m = re.search(pat, body, re.IGNORECASE)
        if m:
            inner = html.escape(m.group(0), quote=False)
            wrapped = f'<a href="{esc_href}">{inner}</a>'
            return body[: m.start()] + wrapped + body[m.end() :]

    # Bare URL pasted as text.
    if url in body:
        link_text = html.escape("View post", quote=False)
        anchor = f'<a href="{esc_href}">{link_text}</a>'
        return body.replace(url, anchor, 1)

    # Last resort: single line before Best regards / closing.
    anchor = f'<a href="{esc_href}">{html.escape("View post", quote=False)}</a>'
    m = re.search(
        r"\n\s*(Best regards|Best,|Cheers,)\b",
        body,
        re.IGNORECASE,
    )
    if m:
        return body[: m.start()] + "\n" + anchor + "\n" + body[m.start() + 1 :].lstrip()
    return body.rstrip() + f"\n\n{anchor}"


def _adjust_max_length_from_request(max_length: int | None) -> int | None:
    """
    Client-provided cap is reduced by 50 for runtime limits (padding for tags/sign-off).
    If unset, leave unset. Result is at least 1 when a cap was provided.
    """
    if max_length is None:
        return None
    return max(1, int(max_length) - 50)


def _is_short_character_limit(max_length: int | None) -> bool:
    """When max_length is 300 or below, responses omit HTML hyperlinks."""
    return isinstance(max_length, int) and max_length <= 300


def _strip_anchor_tags_from_body(body: str) -> str:
    """Replace <a href="...">label</a> with label (plain text)."""
    if not body:
        return body

    def _inner(m: re.Match) -> str:
        raw = m.group(1)
        return html.unescape(raw)

    return re.sub(
        r"<a\b[^>]*>(.*?)</a>",
        _inner,
        body,
        flags=re.IGNORECASE | re.DOTALL,
    )


def _strip_anchors_from_messages(messages: Dict[str, str]) -> Dict[str, str]:
    return {k: _strip_anchor_tags_from_body(v) for k, v in messages.items()}


def _system_prompt_for_length(system_prompt: str, max_length: int | None) -> str:
    """
    For short-message mode (<=300 chars), remove hyperlink-specific instructions
    from prompts so the model doesn't try to format/mention anchors.
    """
    if not _is_short_character_limit(max_length):
        return system_prompt
    return system_prompt.replace(POST_LINK_HTML_INLINE, "")


def _apply_post_link_fallback(
    messages: Dict[str, str], post_link_url: str
) -> Dict[str, str]:
    if not post_link_url:
        return messages
    return {
        k: _ensure_post_link_fallback_body(v, post_link_url)
        for k, v in messages.items()
    }


def _finalize_message_bodies(
    messages: Dict[str, str],
    *,
    post_link_url: str,
    max_length: int | None,
) -> Dict[str, str]:
    """Apply link fallback unless character cap is short; then strip any <a> tags."""
    if _is_short_character_limit(max_length):
        out = _strip_anchors_from_messages(messages)
        return out
    return _apply_post_link_fallback(messages, post_link_url)


async def call_gpt_4_1_with_processing(
    system_prompt,
    user_prompt,
    extract_subject: bool = False,
    max_length: int | None = None,
):
    """Returns message string, or (message_text, subject_line) when extract_subject=True."""
    length_constraint = ""
    if isinstance(max_length, int) and max_length > 0:
        length_constraint = (
            f"\n\nLength rule: Keep only the <message> content within {max_length} "
            "characters, including spaces and punctuation."
        )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"All_Info:\n{user_prompt}{length_constraint}"},
    ]
    retries = 3
    for i in range(retries):
        try:
            response = await invoke(
                messages=messages,
                model="openai/gpt-4.1",
                temperature=0.1,
                fallbacks=[
                    "openai/gpt-4.1-mini",
                    "anthropic/claude-3-5-haiku-latest",
                    "openai/gpt-4o",
                ],
            )
            msg = extract_generic("<message>", "</message>", response)
            if msg:
                msg = msg.strip()
            msg = msg or ""
            if extract_subject:
                subject = (
                    extract_generic("<subject>", "</subject>", response)
                    if response
                    else ""
                )
                return (msg, (subject or "").strip())
            return msg
        except Exception as exc:
            logger.debug("Connection message LLM call failed: %s", exc, exc_info=True)
    logger.debug("Connection message LLM call failed after retries")
    return ("", "") if extract_subject else ""


def parse_post(post: dict, recipient: dict | None = None) -> str:
    """
    Normalize any post object (repost, individual post, or company post)
    into a string with all relevant details for LLM message generation.
    Makes it explicit who the message should be written to.
    """
    raw_link = post.get("link")
    if isinstance(raw_link, str):
        post_link = raw_link.strip()
    elif raw_link is not None:
        post_link = str(raw_link).strip()
    else:
        post_link = ""
    link_line = f"Post link: {post_link}\n" if post_link else ""

    post_type = str(post.get("postType") or "unknown").lower()
    published = post.get("publishedDateRelative") or "Unknown"
    type = str(post.get("entityType") or "unknown").lower()
    news_type = str(post.get("newsType") or "").lower()

    if type == "person_predictive_trigger" or news_type == "predictive_profile_trigger":
        predictive_published = post.get("publishedDateRelative") or published
        evidence = post.get("predictiveTriggerEvidence") or []
        if isinstance(evidence, list):
            evidence = "\n".join([f"- {item}" for item in evidence if item])
        else:
            evidence = str(evidence).strip()
        evidence_line = f"Evidence:\n{evidence}\n" if evidence else ""
        reasoning_line = (
            f"Reasoning: {post.get('predictiveTriggerReasoning')}\n"
            if post.get("predictiveTriggerReasoning")
            else ""
        )
        person_angle_line = (
            f"Person Outreach Angle: {post.get('predictiveTriggerPersonOutreachAngle')}\n"
            if post.get("predictiveTriggerPersonOutreachAngle")
            else ""
        )
        manager_angle_line = (
            f"Stakeholder Outreach Angle: {post.get('predictiveTriggerManagerOutreachAngle')}\n"
            if post.get("predictiveTriggerManagerOutreachAngle")
            else ""
        )
        relevance_line = (
            f"Search Relevance: {post.get('predictiveTriggerSearchRelevance')}\n"
            if post.get("predictiveTriggerSearchRelevance")
            else ""
        )
        people_to_reach_out = post.get("peopleToReachOut") or []
        if not isinstance(people_to_reach_out, list):
            people_to_reach_out = []

        selected_recipient = recipient if isinstance(recipient, dict) else None
        reference_profile_id = str(
            post.get("profileId") or post.get("entityId") or ""
        ).strip()
        recipient_profile_id = (
            str(selected_recipient.get("profileId") or "").strip()
            if selected_recipient
            else ""
        )
        is_original_recipient = (
            not selected_recipient
            or bool(selected_recipient.get("isOriginalProfile"))
            or (
                bool(reference_profile_id)
                and recipient_profile_id == reference_profile_id
            )
        )

        people_line = ""
        if people_to_reach_out:
            rows = []
            for person in people_to_reach_out[:4]:
                if not isinstance(person, dict):
                    continue
                profile_label = (
                    person.get("fullName")
                    or person.get("name")
                    or person.get("publicIdentifier")
                    or person.get("profileId")
                )
                title_label = (
                    person.get("title")
                    or person.get("currentTitle")
                    or person.get("outreachRole")
                    or (
                        "original profile"
                        if person.get("isOriginalProfile")
                        else "stakeholder"
                    )
                )
                rows.append(f"- {profile_label} ({title_label})")
            people_line = (
                "People To Reach Out:\n" + "\n".join(rows) + "\n" if rows else ""
            )

        selected_lines = ""
        selected_cta = ""
        selected_cta_source = "none"
        if selected_recipient:
            recipient_ctas = selected_recipient.get("ctas") or []
            selected_cta = str(selected_recipient.get("selectedCTA") or "").strip()
            if selected_cta:
                selected_cta_source = "selectedCTA"
            if not selected_cta and isinstance(recipient_ctas, list) and recipient_ctas:
                selected_cta = str(recipient_ctas[0]).strip()
                selected_cta_source = "first_cta"
            if not selected_cta:
                selected_cta = "Open a discreet leadership market conversation."
                selected_cta_source = "generic_fallback"
            selected_lines = (
                (
                    "Selected Recipient Reasoning: "
                    f"{selected_recipient.get('reasoning')}\n"
                )
                if selected_recipient.get("reasoning")
                else ""
            )
            selected_lines += f"Selected Recipient CTA: {selected_cta}\n"

        reference_name = post.get("lastAuthorName") or post.get("author")
        reference_role = post.get("lastAuthorDescription") or post.get(
            "authorDescription"
        )
        if not is_original_recipient:
            selected_name = (
                selected_recipient.get("fullName")
                or selected_recipient.get("name")
                or selected_recipient.get("publicIdentifier")
                or ""
            )
            selected_role = (
                selected_recipient.get("title")
                or selected_recipient.get("currentTitle")
                or selected_recipient.get("outreachRole")
                or ""
            )
            recipient_line = (
                f"Message Recipient: {selected_name} ({selected_role or 'stakeholder'})\n"
                if selected_name
                else f"Message Recipient: Stakeholder ({selected_role or 'role unavailable'})\n"
            )
            selected_id_line = (
                f"Recipient Profile ID: {recipient_profile_id}\n"
                if recipient_profile_id
                else ""
            )
            reference_line = (
                f"Reference Executive: {reference_name} ({reference_role})\n"
            )
            situation = "Predictive Stakeholder Outreach"
        else:
            recipient_line = f"Message Recipient: {reference_name} ({reference_role})\n"
            selected_id_line = ""
            reference_line = ""
            situation = "Predictive Profile Trigger Outreach"

        logger.debug(
            "Predictive message parsed: situation=%s reference_profile_id=%s "
            "recipient_profile_id=%s selected_cta_source=%s selected_cta=%s "
            "people_to_reach_out_count=%s trigger_type=%s",
            situation,
            reference_profile_id,
            recipient_profile_id or None,
            selected_cta_source,
            selected_cta or None,
            len(people_to_reach_out),
            post.get("predictiveTriggerType") or post.get("newsSubType"),
        )

        return (
            f"Message Situation: {situation}\n"
            f"{recipient_line}"
            f"{selected_id_line}"
            f"{reference_line}"
            f"Post Type: Predictive Profile Trigger\n"
            f"Trigger Type: {post.get('predictiveTriggerType') or post.get('newsSubType') or 'N/A'}\n"
            f"Trigger Title: {post.get('newsTitle')}\n"
            f"Trigger Description: {post.get('content')}\n"
            f"{reasoning_line}"
            f"{evidence_line}"
            f"Outreach Audience: {post.get('predictiveTriggerOutreachAudience')}\n"
            f"{person_angle_line}"
            f"{manager_angle_line}"
            f"{relevance_line}"
            f"{selected_lines}"
            f"{people_line}"
            f"Published: {predictive_published}\n"
            f"{link_line}"
            f"News Type: {post.get('newsType') or 'N/A'}\n"
        )

    if type == "person_linkedin" and post_type == "repost":
        # reposter = post.get("reposter", {})
        # original = post.get("original_post", {})
        # author = original.get("author", {})
        # company = original.get("company", {})

        return (
            f"Message Recipient: {post.get('lastAuthorName')} ({post.get('lastAuthorDescription')})\n"
            f"Post Type: Repost\n"
            f"Original Author: {post.get('author')} ({post.get('authorDescription')})\n"
            f"Post Content: {post.get('content')}\n"
            f"Published: {published}\n"
            f"{link_line}"
            f"News Type: {post.get('newsType', 'N/A')}\n"
        )

    elif type == "person_linkedin" and post_type == "original":

        return (
            f"Message Recipient: {post.get('lastAuthorName')} ({post.get('lastAuthorDescription')})\n"
            f"Post Type: Individual Post\n"
            f"Post Content: {post.get('content')}\n"
            f"Published: {published}\n"
            f"{link_line}"
        )

    elif type == "company_search":
        associated_user = (
            post.get("associatedUsers", [{}])[0] if post.get("associatedUsers") else {}
        )
        return (
            f"Message Recipient: {associated_user.get('fullName')} who is ({associated_user.get('title')})\n"
            f"Post Type: Company's Post\n"
            f"""Company: {post.get("companyName", "")} - News Title: {post.get("newsTitle", "")}\n"""
            f"Post Content: {post.get('content')}\n"
            f"Published: {published}\n"
            f"{link_line}"
            f"News Type: {post.get('newsType', 'N/A')}\n"
        )

    return ""


async def main(
    payload,
    channel: str = "linkedin_message",
    recipient: dict | None = None,
    sender_name: str | None = None,
    max_length: int | None = None,
):
    max_length = _adjust_max_length_from_request(max_length)

    cleaned_information = parse_post(payload, recipient=recipient)
    if not cleaned_information:
        return {
            "message": {
                "catch_up": "",
                "support": "",
                "congratulate": "",
                "suggested": "",
            },
            "subject_line": None,
        }

    prompt_context = ""
    message_context = "standard"
    if "Message Situation: Predictive Stakeholder Outreach" in cleaned_information:
        prompt_context = PREDICTIVE_STAKEHOLDER_MESSAGE_HINT + "\n"
        message_context = "predictive_stakeholder"
    elif (
        "Message Situation: Predictive Profile Trigger Outreach" in cleaned_information
    ):
        prompt_context = PREDICTIVE_TRIGGER_MESSAGE_HINT + "\n"
        message_context = "predictive_profile_trigger"

    post_link_url = _resolve_payload_link(payload)

    logger.debug(
        "Connection message generation started: channel=%s context=%s "
        "recipient_profile_id=%s max_length=%s post_link_present=%s",
        channel,
        message_context,
        recipient.get("profileId") if isinstance(recipient, dict) else None,
        max_length,
        bool(post_link_url),
    )

    if sender_name:
        cleaned_information = (
            cleaned_information
            + "\n\nSender name (use for sign-off): "
            + sender_name
            + ". End the message with Best regards followed by this name."
        )

    if (
        channel == "linkedin_message"
        and not prompt_context
        and not _is_short_character_limit(max_length)
    ):
        cleaned_information = LINKEDIN_MESSAGE_CHANNEL_HINT + "\n" + cleaned_information

    logger.debug(
        "Connection message prompt context prepared: context=%s all_info_chars=%s",
        message_context,
        len(cleaned_information),
    )

    if channel == "email":
        if prompt_context:
            email_prompts = [
                _system_prompt_for_length(
                    prompt_context
                    + PREDICTIVE_EMAIL_MESSAGE_PROMPT_TEMPLATE.format(
                        intent="catch_up - soft market-mapping conversation"
                    ),
                    max_length,
                ),
                _system_prompt_for_length(
                    prompt_context
                    + PREDICTIVE_EMAIL_MESSAGE_PROMPT_TEMPLATE.format(
                        intent="support - offer useful market perspective"
                    ),
                    max_length,
                ),
                _system_prompt_for_length(
                    prompt_context
                    + PREDICTIVE_EMAIL_MESSAGE_PROMPT_TEMPLATE.format(
                        intent="congratulate - warm acknowledgement only if supported"
                    ),
                    max_length,
                ),
                _system_prompt_for_length(
                    prompt_context
                    + PREDICTIVE_EMAIL_MESSAGE_PROMPT_TEMPLATE.format(
                        intent="suggested - strongest natural CTA"
                    ),
                    max_length,
                ),
            ]
        else:
            email_prompts = [
                _system_prompt_for_length(email_prompt_catch_up, max_length),
                _system_prompt_for_length(email_prompt_offer_support, max_length),
                _system_prompt_for_length(email_prompt_congratulate, max_length),
                _system_prompt_for_length(email_prompt_suggested_message, max_length),
            ]
        results = await asyncio.gather(
            *[
                call_gpt_4_1_with_processing(
                    p,
                    cleaned_information,
                    extract_subject=True,
                    max_length=max_length,
                )
                for p in email_prompts
            ]
        )
        msg = _finalize_message_bodies(
            {
                "catch_up": results[0][0],
                "support": results[1][0],
                "congratulate": results[2][0],
                "suggested": results[3][0],
            },
            post_link_url=post_link_url,
            max_length=max_length,
        )
        return {
            "message": msg,
            "subject_line": {
                "catch_up": results[0][1],
                "support": results[1][1],
                "congratulate": results[2][1],
                "suggested": results[3][1],
            },
        }

    # linkedin_message (default): no subject line
    if prompt_context:
        linkedin_prompts = [
            PREDICTIVE_LINKEDIN_MESSAGE_PROMPT_TEMPLATE.format(
                intent="catch_up - soft market-mapping conversation"
            ),
            PREDICTIVE_LINKEDIN_MESSAGE_PROMPT_TEMPLATE.format(
                intent="support - offer useful market perspective"
            ),
            PREDICTIVE_LINKEDIN_MESSAGE_PROMPT_TEMPLATE.format(
                intent="congratulate - warm acknowledgement only if supported"
            ),
            PREDICTIVE_LINKEDIN_MESSAGE_PROMPT_TEMPLATE.format(
                intent="suggested - strongest natural CTA"
            ),
        ]
    else:
        linkedin_prompts = [
            linkedin_message_prompt_catch_up,
            linkedin_message_prompt_offer_support,
            linkedin_message_prompt_congratulate,
            linkedin_message_prompt_suggested_message,
        ]
    messages_result = await asyncio.gather(
        *[
            call_gpt_4_1_with_processing(
                _system_prompt_for_length(prompt_context + item, max_length),
                cleaned_information,
                extract_subject=False,
                max_length=max_length,
            )
            for item in linkedin_prompts
        ]
    )
    catch_up, support, congratulate, suggested_message = messages_result

    msg = _finalize_message_bodies(
        {
            "catch_up": catch_up,
            "support": support,
            "congratulate": congratulate,
            "suggested": suggested_message,
        },
        post_link_url=post_link_url,
        max_length=max_length,
    )
    return {
        "message": msg,
        "subject_line": None,
    }
