"""Fetch and flatten Vinted's category tree for the category picker.

Vinted has no documented `/catalogs` API endpoint (checked — 404s under both
the mobile-app and browser header profiles). The full, localized category
tree does exist, though: it's embedded in the homepage HTML as a React
Server Components streaming chunk (`self.__next_f.push([1, "..."])`) whose
payload contains a `"catalogTree": [...]` JSON array. This module pulls
that out and flattens it into a simple id/title/path list.
"""

import json
import time
import threading
import requests

from config import VINTED_BASE_URL

_DESKTOP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "pl-PL,pl;q=0.9",
}

_PUSH_PREFIX = 'self.__next_f.push([1,"'
_PUSH_SUFFIX = '"])</script>'
_TREE_KEY = '"catalogTree":['

_CACHE_TTL_SECONDS = 24 * 3600

_cache_lock = threading.Lock()
_cache: list[dict] | None = None
_cache_time = 0.0


def get_categories(force_refresh: bool = False) -> list[dict]:
    """Return a flat list of {id, title, path} for every Vinted category.

    Cached in-memory for _CACHE_TTL_SECONDS since the category tree barely
    ever changes and re-parsing it means fetching and scanning ~2MB of HTML.
    """
    global _cache, _cache_time
    with _cache_lock:
        if _cache is not None and not force_refresh and (time.time() - _cache_time) < _CACHE_TTL_SECONDS:
            return _cache

        resp = requests.get(VINTED_BASE_URL + "/", headers=_DESKTOP_HEADERS, timeout=20)
        resp.raise_for_status()
        tree = _extract_catalog_tree(resp.text)
        flat = _flatten(tree)

        _cache = flat
        _cache_time = time.time()
        return flat


def _extract_catalog_tree(html: str) -> list[dict]:
    """Find the streaming chunk carrying `catalogTree` and parse it out."""
    idx = html.find(_PUSH_PREFIX)
    while idx != -1:
        end = html.find(_PUSH_SUFFIX, idx)
        if end == -1:
            break
        chunk = html[idx + len(_PUSH_PREFIX):end]
        if "catalogTree" in chunk:
            # The chunk is a JS string literal (backslash-escaped) — decode it
            # as a JSON string to get the real, plain-JSON text back.
            real = json.loads('"' + chunk + '"')
            key_idx = real.find(_TREE_KEY)
            if key_idx == -1:
                return []
            array_text = _balanced_array(real, key_idx + len(_TREE_KEY) - 1)
            return json.loads(array_text)
        idx = html.find(_PUSH_PREFIX, end)
    return []


def _balanced_array(text: str, start: int) -> str:
    """Return the bracket-balanced JSON array substring starting at text[start] == '['."""
    depth = 0
    in_str = False
    esc = False
    i = start
    while i < len(text):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        i += 1
    raise ValueError("Unbalanced catalog tree array in Vinted homepage payload")


def _flatten(tree: list[dict], parent_path: str = "") -> list[dict]:
    flat = []
    for node in tree:
        title = node.get("title", "")
        path = f"{parent_path} › {title}" if parent_path else title
        flat.append({"id": node.get("id"), "title": title, "path": path})
        flat.extend(_flatten(node.get("catalogs") or [], path))
    return flat
