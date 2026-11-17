"""
Assign High / Medium / Low from `total_score` using mean, standard deviation, and ranks.

**Cohort for μ and σ:** only the **top 20** connections by ``total_score`` (highest first).
Mean, sample standard deviation, z-scores, and tertile labels all use this cohort only—never a
wider list, even if the caller passes more rows.

Approach (no LLM):
1. Keep rows with a non-empty ``connection_public_identifier``, sort by ``total_score`` descending,
   take at most :data:`ZSCORE_COHORT_MAX` (20). Drop the rest before any statistics.
2. For each row in that cohort, take ``total_score`` as *x*.
3. Compute the sample mean *μ* and sample standard deviation *σ* (``statistics.stdev``) **on that
   cohort only**.
4. **Z-score**: *z* = (*x* − *μ*) / *σ* when *σ* > 0; if *σ* = 0 (identical scores), every connection
   gets **Medium** (except the single-row case below).
5. Sort connections by *z* descending, breaking ties by raw ``total_score``, then stable index.
6. **Tertiles**: split the sorted cohort into three bands — top third **High**, middle **Medium**,
   bottom **Low** (sizes use ceil/floor so counts sum to *n*).

Edge cases:
- **One connection** in the cohort: **High** (only candidate).
- **Identical scores** (*σ* = 0): all **Medium**.
"""

from __future__ import annotations

# Max rows that participate in μ, σ, z-scores, and priority bands (must match BD “top” window).
ZSCORE_COHORT_MAX = 20

import logging
import statistics
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


def _float_score(row: Dict[str, Any]) -> float:
    try:
        return float(row.get("total_score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def assign_priorities_zscore(rows: List[Dict[str, Any]]) -> List[Tuple[str, str]]:
    """
    Return ``(connection_public_identifier, priority)`` for each row in the scored cohort.

    Statistics (mean, stdev, z) use **only** the top :data:`ZSCORE_COHORT_MAX` rows by score.
    Priority is one of ``\"High\"``, ``\"Medium\"``, ``\"Low\"`` (strings matching DB / enum).
    """
    items: List[Tuple[str, float]] = []
    for r in rows:
        cid = str(r.get("connection_public_identifier") or "").strip()
        if not cid:
            continue
        items.append((cid, _float_score(r)))

    if not items:
        return []

    # μ and σ are defined only on the highest-scoring connections (top 20), not the full input.
    items.sort(key=lambda t: t[1], reverse=True)
    items = items[:ZSCORE_COHORT_MAX]

    n = len(items)
    if n == 1:
        return [(items[0][0], "High")]

    scores = [s for _, s in items]
    mu = statistics.mean(scores)
    sigma = statistics.stdev(scores)

    if sigma == 0.0:
        logger.info(
            "assign_priorities_zscore: all %d scores equal (sigma=0), assigning all Medium",
            n,
        )
        return [(cid, "Medium") for cid, _ in items]

    # z-score per row (same order as items)
    zs = [(score - mu) / sigma for _, score in items]

    # Sort by z desc, then score desc, then stable index
    order = sorted(
        range(n),
        key=lambda i: (-zs[i], -items[i][1], i),
    )

    n_high = (n + 2) // 3  # ceil(n/3)
    n_low = n // 3  # floor(n/3)
    n_mid = n - n_high - n_low

    labels = ["Medium"] * n
    for rank, idx in enumerate(order):
        if rank < n_high:
            labels[idx] = "High"
        elif rank < n_high + n_mid:
            labels[idx] = "Medium"
        else:
            labels[idx] = "Low"

    return [(items[i][0], labels[i]) for i in range(n)]
