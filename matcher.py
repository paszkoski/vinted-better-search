"""Keyword matching against item title + description.

Vinted's own search is fuzzy/semantic — it happily returns items whose
title and description don't contain the words you typed. This module
implements strict, literal matching instead:

  - keywords are comma-separated AND groups: every group must appear as
    a substring of title+description (case-insensitive) — OR across the
    two fields
  - within a group, alternatives joined by " OR " are OR'd together, so
    "keychron OR key chron, k3" means (keychron OR key chron) AND k3
  - if any exclude keyword appears in title+description, the item is
    rejected, even if it also matches every include group
"""

import re

_OR_SPLIT = re.compile(r"\s+or\s+", re.IGNORECASE)


def parse_keyword_groups(raw: str) -> list[list[str]]:
    """Split a comma-separated keyword string into AND-groups of lowercase,
    trimmed OR-alternatives. "keychron OR key chron, k3" ->
    [["keychron", "key chron"], ["k3"]]."""
    if not raw:
        return []
    groups = []
    for part in raw.split(","):
        alts = [alt.strip().lower() for alt in _OR_SPLIT.split(part) if alt.strip()]
        if alts:
            groups.append(alts)
    return groups


def parse_keywords(raw: str) -> list[str]:
    """Flat list of every term in `raw`, ignoring AND/OR structure — used
    for exclude keywords, where any single match is enough to reject."""
    return [term for group in parse_keyword_groups(raw) for term in group]


def title_satisfies(title: str, include_groups: list[list[str]], exclude_terms: list[str]) -> bool | None:
    """
    Check a title alone against include/exclude terms.

    Returns:
        True  — title already satisfies every include group and no exclude term (no fetch needed)
        False — title alone contains an exclude term (reject outright, no fetch needed)
        None  — inconclusive (title is missing some include group) — description must be checked
    """
    title_l = title.lower()
    if any(term in title_l for term in exclude_terms):
        return False
    if all(any(alt in title_l for alt in group) for group in include_groups):
        return True
    return None


def full_satisfies(title: str, description: str, include_groups: list[list[str]], exclude_terms: list[str]) -> bool:
    """Check title+description together against include/exclude terms."""
    combined = f"{title.lower()} {description.lower()}"
    if any(term in combined for term in exclude_terms):
        return False
    return all(any(alt in combined for alt in group) for group in include_groups)
