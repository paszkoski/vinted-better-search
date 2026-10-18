"""Keyword matching against item listing fields or title + description.

Vinted's own search is fuzzy/semantic — it happily returns items whose
title and description don't contain the words you typed. This module
implements strict, literal matching instead. Matching happens in two
stages, because an item description costs a whole extra request:

  - quick search matches the listing fields already in the search response
    (title + brand) — see `listing_satisfies()`. Anything the listing
    alone can't settle is simply not a quick-search result, unless the
    caller opts into "partial matches" (`missing_groups()`), which surfaces
    candidates matching only some include groups so they can be checked
    against the description.
  - deep search (opt-in, per selected item) fetches the item page and
    matches title + description together — see `full_satisfies()`.

Both stages use the same keyword rules:

  - keywords are comma-separated AND groups: every group must appear as
    a substring of the matched text (case-insensitive)
  - within a group, alternatives joined by " OR " are OR'd together, so
    "keychron OR key chron, k3" means (keychron OR key chron) AND k3
  - if any exclude keyword appears in the matched text, the item is
    rejected, even if it also matches every include group — and if any
    exclude term appears only in the description, deep search must still
    be able to say which term that was (see `first_exclude_hit()`)
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


def listing_satisfies(text: str, include_groups: list[list[str]], exclude_terms: list[str]) -> bool:
    """Strict match against listing-only text (title + brand) — the fields a
    Vinted search response already contains, so deciding this costs no extra
    request. Unlike `title_satisfies`, there is no inconclusive third answer:
    true only if every include group is present and no exclude term is."""
    text_l = text.lower()
    if any(term in text_l for term in exclude_terms):
        return False
    return all(any(alt in text_l for alt in group) for group in include_groups)


def first_exclude_hit(text: str, exclude_terms: list[str]) -> str | None:
    """First exclude term found in `text` (or None). Deep search reports the
    offending term back so the card can say *why* it was excluded, rather
    than just dimming with no explanation."""
    text_l = text.lower()
    return next((term for term in exclude_terms if term in text_l), None)


def missing_groups(text: str, include_groups: list[list[str]]) -> list[list[str]]:
    """Include groups with no alternative present in `text`, in order.

    Quick search uses this to tell two very different non-matches apart:
    a *partial* candidate (some groups matched, so it's plausibly relevant —
    the missing ones may live in the description) versus noise from Vinted's
    fuzzy pool (no group matched at all). The group list is also what the
    frontend shows as "Partial — missing: …"."""
    text_l = text.lower()
    return [group for group in include_groups if not any(alt in text_l for alt in group)]
