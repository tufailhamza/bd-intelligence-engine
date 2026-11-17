import json
import re

from typing import Dict, List, Any


def clean_json_from_llm_output(json_like_output: str):
    """
    Cleans and parses a string that looks like a JSON code block, removing
    triple backticks and loading it into a Python dict.

    Returns a Python dict or None if parsing fails.
    """
    if not json_like_output:
        return None

    lines = json_like_output.splitlines()
    if lines and lines[0].startswith("```json"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    cleaned_str = "\n".join(lines)
    try:
        return json.loads(cleaned_str)
    except json.JSONDecodeError:
        return None


def parse_llm_merge_response(response_text):
    """
    Parses the XML-like response into a list of merge instructions:
    [{'ids': [...], 'new_name': ..., 'new_description': ..., 'reason': ..., 'explanation': ...}, ...]
    """
    import re

    pattern = re.compile(
        r'<CLUSTER ID="(?P<ids>[^"]+)">.*?'
        r"<REASON>(?P<reason>.*?)</REASON>.*?"
        r"<NEW_DESCRIPTION>(?P<new_description>.*?)</NEW_DESCRIPTION>.*?"
        r"<NEW_NAME>(?P<new_name>.*?)</NEW_NAME>.*?"
        r"<EXPLANATION>(?P<explanation>.*?)</EXPLANATION>.*?</CLUSTER>",
        re.DOTALL,
    )

    instructions = []
    for match in pattern.finditer(response_text):
        ids = [id_.strip() for id_ in match.group("ids").split(",")]
        reason = match.group("reason").strip()
        new_description = match.group("new_description").strip()
        new_name = match.group("new_name").strip()
        explanation = match.group("explanation").strip()

        instructions.append(
            {
                "ids": ids,
                "reason": reason,
                "new_description": new_description,
                "new_name": new_name,
                "explanation": explanation,
            }
        )

    return instructions


GENERIC_INDUSTRY_WATCH_LIST = [
    "Information Technology & Services",
    "Technology, Information and Internet",
    "Software Development",
    "Financial Services",
    "Hospitals and Health Care",
    "Manufacturing",
    "Retail",
    "Education",
    "Entertainment Providers",
    "Professional Services",
    "Business Consulting and Services",
    "IT Services and IT Consulting",
]


TITLE_NORMALIZATION_MAP = {
    # Senior,Junior level
    "sr": "senior",
    "sr.": "senior",
    "snr": "senior",
    "snr.": "senior",
    "senoir": "senior",
    "jr": "junior",
    "jr.": "junior",
    "jrn": "junior",
    "jnr": "junior",
    # C-level titles
    "ceo": "chief executive officer",
    "ceo.": "chief executive officer",
    "ceeo": "chief executive officer",
    "co-ceo": "co chief executive officer",
    "cfo": "chief financial officer",
    "coo": "chief operating officer",
    "coo.": "chief operating officer",
    "cto": "chief technology officer",
    "cto.": "chief technology officer",
    "cmo": "chief marketing officer",
    "cio": "chief information officer",
    "cso": "chief strategy officer",
    "cpo": "chief product officer",
    "cco": "chief commercial officer",
    "chro": "chief human resources officer",
    "cro": "chief revenue officer",
    "cao": "chief administrative officer",
    "cdo": "chief data officer",
    "cko": "chief knowledge officer",
    "ciso": "chief information security officer",
    "cxo": "chief experience officer",
    "chief exec": "chief executive officer",
    "chief tech": "chief technology officer",
    "chief info": "chief information officer",
    "chief hr": "chief human resources officer",
    "chief ops": "chief operating officer",
    # Founders
    "co-founder": "co founder",
    "cofounder": "co founder",
    "co founder": "co founder",
    # Vice presidents and directors
    "vp": "vice president",
    "vp.": "vice president",
    "v.p.": "vice president",
    "v.p": "vice president",
    "svp": "senior vice president",
    "avp": "assistant vice president",
    "evp": "executive vice president",
    "vp hr": "vice president human resources",
    "vp marketing": "vice president marketing",
    "vp sales": "vice president sales",
    "vp talent": "vice president talent acquisition",
    "dir": "director",
    "dir.": "director",
    "exec dir": "executive director",
    "exec director": "executive director",
    "deputy dir": "deputy director",
    "assoc dir": "associate director",
    "associate dir": "associate director",
    "managing dir": "managing director",
    "managing director": "managing director",
    "deputy md": "deputy managing director",
    "principal dir": "principal director",
    "regional dir": "regional director",
    "global dir": "global director",
    # Board roles
    "board member": "board member",
    "board dir": "board director",
    "non-exec dir": "non executive director",
    "nonexec dir": "non executive director",
    "nex dir": "non executive director",
    # Manager roles
    "mgr": "manager",
    "mgr.": "manager",
    "mng": "manager",
    "mngr": "manager",
    "mgmt": "management",
    "mngt": "management",
    "tech mgr": "technical manager",
    "ops mgr": "operations manager",
    "sales mgr": "sales manager",
    "bd mgr": "business development manager",
    # Technical roles
    "dev": "developer",
    "dev.": "developer",
    "frontend": "front end",
    "back-end": "back end",
    "backend": "back end",
    "fullstack": "full stack",
    "full-stack": "full stack",
    "swe": "software engineer",
    "se": "software engineer",
    "eng": "engineer",
    "eng.": "engineer",
    "qa": "quality assurance",
    "qa eng": "quality assurance engineer",
    "sr dev": "senior developer",
    "sr eng": "senior engineer",
    "sde": "software development engineer",
    "tech lead": "technical lead",
    "techlead": "technical lead",
    "lead dev": "lead developer",
    "lead eng": "lead engineer",
    # Product / Project roles
    "po": "product owner",
    "pm": "product manager",
    "prod mgr": "product manager",
    "proj mgr": "project manager",
    "program mgr": "program manager",
    # Business & Operations
    "biz dev": "business development",
    "bd": "business development",
    "ops": "operations",
    "opr": "operations",
    "admin": "administrator",
    "adm": "administrator",
    "coordinator": "coordinator",
    "analyst": "analyst",
    "ba": "business analyst",
    "fa": "financial analyst",
    "pa": "personal assistant",
    # Consultant roles
    "consult": "consultant",
    "consult.": "consultant",
    "conslt": "consultant",
    "cons.": "consultant",
    "assoc": "associate",
    "assc": "associate",
    "principal cons": "principal consultant",
    "sr consultant": "senior consultant",
    "lead consultant": "lead consultant",
    # Sales / Marketing / HR
    "acct mgr": "account manager",
    "acct exec": "account executive",
    "sales rep": "sales representative",
    "biz analyst": "business analyst",
    "mktg": "marketing",
    "mkt": "marketing",
    "hr": "human resources",
    "recruiter": "recruiter",
    "ta": "talent acquisition",
    # Executive Recruitment & Talent
    "exec search": "executive search",
    "exec recruiter": "executive recruiter",
    "exec recruitment": "executive recruitment",
    "exec talent": "executive talent",
    "recruitment cons": "recruitment consultant",
    "recruitment consultant": "recruitment consultant",
    "recruitment lead": "recruitment lead",
    "recruitment head": "recruitment head",
    "recruitment mgr": "recruitment manager",
    "recruitment specialist": "recruitment specialist",
    "talent mgr": "talent manager",
    "talent partner": "talent acquisition partner",
    "talent head": "talent acquisition head",
    # Misc
    "spec": "specialist",
    "intern": "intern",
    "grad": "graduate",
    "trainee": "trainee",
    "officer": "officer",
    "head": "head",
    "lead": "lead",
}
