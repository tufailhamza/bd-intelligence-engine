"""
Impact ranking computation for connections.

Combines management level (seniority) with meeting frequency to derive
one of three tiers: Strong | Moderate | Weak.

Scoring matrix
--------------
                  Many meetings (≥3)  Some meetings (1-2)  No meetings (0)
 Senior   (0-2)       Strong               Strong              Moderate
 Mid-Sr   (3-5)       Strong               Moderate            Weak
 Mid      (6-9)       Moderate             Weak                Weak
 Junior  (10-12)      Weak                 Weak                Weak

Management level reference
--------------------------
 0  Executive & Sr. VP      4  Vice President     8  Founder
 1  C-Suite                 5  Head               9  Board Member
 2  Director                6  Manager           10  Senior
 3  President               7  Partner           11  Mid-Level
                                                 12  Junior
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IMPACT_STRONG = "Strong"
IMPACT_MODERATE = "Moderate"
IMPACT_WEAK = "Weak"
VALID_IMPACT_VALUES = {IMPACT_STRONG, IMPACT_MODERATE, IMPACT_WEAK}

# Management level → seniority bucket
_SENIOR_LEVELS = {0, 1, 2}  # Executive/Sr. VP, C-Suite, Director
_MID_SENIOR_LEVELS = {3, 4, 5}  # President, VP, Head
_MID_LEVELS = {6, 7, 8, 9}  # Manager, Partner, Founder, Board Member
_JUNIOR_LEVELS = {10, 11, 12}  # Senior, Mid-Level, Junior

# Meeting frequency thresholds
_MANY_MEETINGS = 3
_SOME_MEETINGS = 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_management_levels(profile: Optional[Dict[str, Any]]) -> List[int]:
    """Return management_level values from the top experience entry."""
    if not isinstance(profile, dict):
        return []
    exp = profile.get("experience")
    if not isinstance(exp, list) or not exp or not isinstance(exp[0], dict):
        return []
    ml = exp[0].get("management_level")
    if ml is None:
        return []
    if isinstance(ml, (int, float)):
        return [int(ml)]
    if isinstance(ml, list):
        return [int(x) for x in ml if isinstance(x, (int, float))]
    return []


def _dominant_level_bucket(levels: List[int]) -> str:
    """Return the highest-seniority bucket represented in *levels*."""
    if not levels:
        return "unknown"
    if any(lvl in _SENIOR_LEVELS for lvl in levels):
        return "senior"
    if any(lvl in _MID_SENIOR_LEVELS for lvl in levels):
        return "mid_senior"
    if any(lvl in _MID_LEVELS for lvl in levels):
        return "mid"
    if any(lvl in _JUNIOR_LEVELS for lvl in levels):
        return "junior"
    return "unknown"


def _meeting_bucket(num_meetings: int) -> str:
    if num_meetings >= _MANY_MEETINGS:
        return "many"
    if num_meetings >= _SOME_MEETINGS:
        return "some"
    return "none"


# Lookup table: (level_bucket, meeting_bucket) → impact ranking
_MATRIX: Dict[tuple, str] = {
    ("senior", "many"): IMPACT_STRONG,
    ("senior", "some"): IMPACT_STRONG,
    ("senior", "none"): IMPACT_MODERATE,
    ("mid_senior", "many"): IMPACT_STRONG,
    ("mid_senior", "some"): IMPACT_MODERATE,
    ("mid_senior", "none"): IMPACT_WEAK,
    ("mid", "many"): IMPACT_MODERATE,
    ("mid", "some"): IMPACT_WEAK,
    ("mid", "none"): IMPACT_WEAK,
    ("junior", "many"): IMPACT_WEAK,
    ("junior", "some"): IMPACT_WEAK,
    ("junior", "none"): IMPACT_WEAK,
    ("unknown", "many"): IMPACT_MODERATE,
    ("unknown", "some"): IMPACT_WEAK,
    ("unknown", "none"): IMPACT_WEAK,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_impact_ranking(
    *,
    profile: Optional[Dict[str, Any]],
    num_meetings: int,
) -> str:
    """
    Derive the impact ranking for one connection.

    Args:
        profile:      LinkedIn profile dict (needs ``experience[0].management_level``).
        num_meetings: Number of calendar meetings this connection has with the reference.

    Returns:
        One of "Strong", "Moderate", "Weak".
    """
    levels = _get_management_levels(profile)
    level_bucket = _dominant_level_bucket(levels)
    meeting_bucket = _meeting_bucket(max(0, num_meetings))
    ranking = _MATRIX.get((level_bucket, meeting_bucket), IMPACT_WEAK)
    logger.debug(
        "impact_ranking: levels=%s bucket=%s meetings=%d meeting_bucket=%s -> %s",
        levels,
        level_bucket,
        num_meetings,
        meeting_bucket,
        ranking,
    )
    return ranking


def compute_batch_impact_rankings(
    *,
    profiles: Dict[str, Optional[Dict[str, Any]]],
    meetings_per_connection: Dict[str, int],
) -> Dict[str, str]:
    """
    Compute impact rankings for multiple connections at once.

    Args:
        profiles:                pid -> profile dict
        meetings_per_connection: pid -> number of meetings

    Returns:
        pid -> impact ranking string
    """
    results: Dict[str, str] = {}
    for pid, profile in profiles.items():
        num_meetings = meetings_per_connection.get(pid, 0)
        results[pid] = compute_impact_ranking(
            profile=profile, num_meetings=num_meetings
        )
    return results


def impact_ranking_for_db_row(
    *,
    metric_scores: Optional[Dict[str, Any]],
    profile: Optional[Dict[str, Any]],
) -> str:
    """
    Compute impact_ranking for one ranked row before persisting via insert_batch_rankings.
    """
    return compute_impact_ranking(
        profile=profile,
        num_meetings=extract_meeting_count_from_metric_scores(metric_scores),
    )


def extract_meeting_count_from_metric_scores(
    metric_scores: Optional[Dict[str, Any]],
) -> int:
    """
    Derive meeting count from stored ``metric_scores`` (field: ``meeting_points``).

    meeting_points formula (from simple_connection_ranking):
        first meeting  = 30 pts
        each after     = +15 pts
    So: meetings = (meeting_points - 30) / 15 + 1  for points > 0, else 0.
    """
    if not isinstance(metric_scores, dict):
        return 0
    raw = metric_scores.get("meeting_points")
    try:
        pts = float(raw or 0)
    except (TypeError, ValueError):
        return 0
    if pts <= 0:
        return 0
    if pts <= 30:
        return 1
    return int((pts - 30) / 15) + 1
