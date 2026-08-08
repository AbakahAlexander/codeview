"""Deduplicate and rank entry-point candidates."""

from __future__ import annotations

from codeview.entrypoints.models import EntryPointCandidate
from codeview.models import Confidence, EntryPointKind

# Prefer runnable roots. LIBRARY export surfaces are not entry points — those
# symbols are reached by expanding callers/callees from real roots.
_CATEGORY_RANK = {
    EntryPointKind.CLI: 0,
    EntryPointKind.FRONTEND: 1,
    EntryPointKind.NATIVE_MAIN: 2,
    EntryPointKind.MODULE: 3,
    EntryPointKind.LIBRARY: 4,
    EntryPointKind.INFERRED: 5,
}

_CONFIDENCE_RANK = {
    Confidence.CONFIRMED: 0,
    Confidence.LIKELY: 1,
    Confidence.INFERRED: 2,
}


def rank_candidates(
    candidates: list[EntryPointCandidate], *, limit: int = 40
) -> list[EntryPointCandidate]:
    """Stable dedupe by path/module target; drop LIBRARY export lists only."""
    best: dict[str, EntryPointCandidate] = {}
    order: list[str] = []

    for cand in candidates:
        if cand.category == EntryPointKind.LIBRARY:
            continue
        key = _candidate_key(cand)
        prev = best.get(key)
        if prev is None:
            best[key] = cand
            order.append(key)
            continue
        if _score(cand) < _score(prev):
            best[key] = cand

    ranked = sorted(
        best.values(),
        key=lambda c: (_score(c), order.index(_candidate_key(c))),
    )
    return ranked[:limit]


def _candidate_key(cand: EntryPointCandidate) -> str:
    if cand.module and cand.attr:
        return f"mod:{cand.module}:{cand.attr}"
    if cand.path and cand.attr:
        return f"path:{cand.path}:{cand.attr}"
    if cand.path:
        return f"path:{cand.path}"
    if cand.module:
        return f"mod:{cand.module}"
    return f"name:{cand.display_name}:{cand.source}"


def _score(cand: EntryPointCandidate) -> tuple[int, int, str]:
    return (
        _CONFIDENCE_RANK.get(cand.confidence, 9),
        _CATEGORY_RANK.get(cand.category, 9),
        cand.display_name,
    )
