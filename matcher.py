"""Keyword matching against item title + description.

Vinted's own search is fuzzy/semantic — it happily returns items whose
title and description don't contain the words you typed. This module
implements strict, literal matching instead:

  - every include keyword must appear as a substring of title+description
    (case-insensitive) — AND across keywords, OR across the two fields
  - if any exclude keyword appears in title+description, the item is
    rejected, even if it also matches every include keyword
"""


def parse_keywords(raw: str) -> list[str]:
    """Split a comma-separated keyword string into lowercase, trimmed terms."""
    if not raw:
        return []
    return [term.strip().lower() for term in raw.split(",") if term.strip()]


def title_satisfies(title: str, include_terms: list[str], exclude_terms: list[str]) -> bool | None:
    """
    Check a title alone against include/exclude terms.

    Returns:
        True  — title already satisfies every include term and no exclude term (no fetch needed)
        False — title alone contains an exclude term (reject outright, no fetch needed)
        None  — inconclusive (title is missing some include term) — description must be checked
    """
    title_l = title.lower()
    if any(term in title_l for term in exclude_terms):
        return False
    if all(term in title_l for term in include_terms):
        return True
    return None


def full_satisfies(title: str, description: str, include_terms: list[str], exclude_terms: list[str]) -> bool:
    """Check title+description together against include/exclude terms."""
    combined = f"{title.lower()} {description.lower()}"
    if any(term in combined for term in exclude_terms):
        return False
    return all(term in combined for term in include_terms)
