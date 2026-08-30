#!/usr/bin/env python3
"""
Vinted Scraper — search Vinted's catalog and fetch per-item descriptions.

Vinted's own `catalog/items` API only returns a title for each item — no
description — so its search silently ignores whatever the description
contains. `get_item_description()` fetches the public item page and pulls
the description back out of the JSON-LD `<script type="application/ld+json">`
block Vinted embeds in every item page, which is what makes real
title-or-description keyword matching possible.

Usage (CLI, for manual testing):
    python scraper.py "xreal beam pro"
    python scraper.py "xreal beam pro" --catalog 2994
"""

import json
import time
import uuid
import random
import secrets
import argparse
import threading
import requests
from concurrent.futures import ThreadPoolExecutor

from config import (
    VINTED_BASE_URL,
    VINTED_API_URL,
    DEFAULT_CURRENCY,
    DEFAULT_ORDER,
    CATALOG_PER_PAGE,
    SESSION_REFRESH_INTERVAL,
    BLOCK_WAIT_SECONDS,
    MIN_API_DELAY,
    DETAIL_CONCURRENCY,
    DETAIL_JITTER_SECONDS,
    VINTED_PROXY,
    DESCRIPTION_CACHE_SIZE,
)

# ── Mobile app profiles (from real iOS Vinted app traffic) ────────────────────
# User-Agent format: vinted-ios Vinted/<version> (<bundle>; build:<build>; iOS <ios>) <model>
_APP_VERSION = "26.6.1"
_BUILD = "30214"

MOBILE_PROFILES = [
    {
        "User-Agent": f"vinted-ios Vinted/{_APP_VERSION} (lt.manodrabuziai.pl; build:{_BUILD}; iOS 26.2.1) iPhone16,1",
        "X-Device-Model": "iPhone16,1",
        "x-os-version": "26.2.1",
        "x-screen-width": "1179.0",
        "x-screen-height": "2556.0",
    },
    {
        "User-Agent": f"vinted-ios Vinted/{_APP_VERSION} (lt.manodrabuziai.pl; build:{_BUILD}; iOS 26.2.1) iPhone17,2",
        "X-Device-Model": "iPhone17,2",
        "x-os-version": "26.2.1",
        "x-screen-width": "1290.0",
        "x-screen-height": "2796.0",
    },
    {
        "User-Agent": f"vinted-ios Vinted/{_APP_VERSION} (lt.manodrabuziai.pl; build:{_BUILD}; iOS 26.1.1) iPhone15,4",
        "X-Device-Model": "iPhone15,4",
        "x-os-version": "26.1.1",
        "x-screen-width": "1179.0",
        "x-screen-height": "2556.0",
    },
    {
        "User-Agent": f"vinted-ios Vinted/{_APP_VERSION} (lt.manodrabuziai.pl; build:{_BUILD}; iOS 26.0.1) iPhone16,2",
        "X-Device-Model": "iPhone16,2",
        "x-os-version": "26.0.1",
        "x-screen-width": "1290.0",
        "x-screen-height": "2796.0",
    },
]

_LD_JSON_TAG = 'application/ld+json">'


class VintedScraper:
    """Scraper for Vinted catalog search + item descriptions.

    Each instance is a single fixed identity — its own cookies, device
    profile and throttle/block state, and either always direct or always
    through the proxy (`use_proxy`), never both. To spread requests across
    both a direct and a proxy connection, use `ScraperPool` instead of
    juggling instances yourself.
    """

    def __init__(self, on_blocked=None, use_proxy: bool = False):
        self.session = requests.Session()
        self._api_call_count = 0
        self._request_count = 0  # every HTTP request actually sent to Vinted (incl. retries)
        self._blocked_until = 0.0
        self._last_request_time = 0.0
        # Stable per-instance identifiers (simulate a real device)
        self._device_uuid = secrets.token_hex(16)
        self._anon_id = str(uuid.uuid4())
        self._icloud_id = "_" + secrets.token_hex(16)   # format: _<32 hex chars>
        self._profile = random.choice(MOBILE_PROFILES)
        # Per-session identifiers (rotate on session refresh)
        self._session_id = str(uuid.uuid4())
        self._agent_id = str(uuid.uuid4())
        # Optional callback: on_blocked(wait_minutes: int) — called when a 403 block is detected
        self._on_blocked = on_blocked
        # Fixed for this instance's lifetime — see class docstring
        self._use_proxy = use_proxy and bool(VINTED_PROXY)
        # In-memory description cache: item_id -> description (or "" if none/unavailable)
        self._description_cache: dict[int, str] = {}
        # In-memory seller-country cache: user_id -> {"title": ..., "code": ...} or None
        self._country_cache: dict[int, dict | None] = {}
        self._cache_lock = threading.Lock()
        self._block_lock = threading.Lock()
        # Bounds how many item-description / user-profile fetches run in flight at once
        self._detail_semaphore = threading.Semaphore(DETAIL_CONCURRENCY)
        self._apply_headers()
        self._apply_proxy()
        self._init_session()

    # ── Header / profile management ───────────────────────────────────────────

    def _rotate_profile(self):
        self._profile = random.choice(MOBILE_PROFILES)
        self._session_id = str(uuid.uuid4())
        self._agent_id = str(uuid.uuid4())

    def _apply_headers(self):
        p = self._profile
        self.session.headers.clear()
        self.session.headers.update({
            "X-Session-Id": self._session_id,
            "Accept": "*/*",
            "Locale": "pl-PL",
            "Accept-Language": "pl-PL",
            "Accept-Encoding": "gzip, deflate, br",
            "X-Anon-Id": self._anon_id,
            "X-Device-UUID": self._device_uuid,
            "User-Agent": p["User-Agent"],
            "Connection": "keep-alive",
            "Short-Bundle-Version": _APP_VERSION,
            "X-ICloud-Identifier": self._icloud_id,
            "X-Device-Model": p["X-Device-Model"],
            "X-App-Version": _APP_VERSION,
            "App-Version": "8",
            "x-platform": "iphone",
            "x-os-version": p["x-os-version"],
            "x-portal": "pl",
            "x-agent-id": self._agent_id,
            "x-screen-width": p["x-screen-width"],
            "x-screen-height": p["x-screen-height"],
        })

    def _update_vudt_header(self):
        v_udt = self.session.cookies.get("v_udt")
        if v_udt:
            self.session.headers["X-V-Udt"] = v_udt

    @property
    def request_count(self) -> int:
        return self._request_count

    # ── Block detection / cooldown ────────────────────────────────────────────

    def _is_blocked(self) -> bool:
        return time.time() < self._blocked_until

    def _apply_proxy(self):
        if self._use_proxy:
            proxy_url = f"http://{VINTED_PROXY}"
            self.session.proxies = {"http": proxy_url, "https": proxy_url}
        else:
            self.session.proxies = {}

    def _set_blocked(self):
        with self._block_lock:
            self._blocked_until = time.time() + BLOCK_WAIT_SECONDS
            wait_min = BLOCK_WAIT_SECONDS // 60
            mode = "proxy" if self._use_proxy else "direct"
            print(f"Blocked by Vinted (403) on {mode} connection! Pausing this identity for {wait_min} minutes...")
            if self._on_blocked:
                try:
                    self._on_blocked(wait_min)
                except Exception as e:
                    print(f"Block notification error: {e}")

    # ── Request throttling ────────────────────────────────────────────────────

    def _throttle(self, min_delay: float):
        elapsed = time.time() - self._last_request_time
        if elapsed < min_delay:
            wait = min_delay - elapsed + random.uniform(0.0, 0.4)
            time.sleep(wait)

    # ── Session management ────────────────────────────────────────────────────

    def _init_session(self):
        if self._is_blocked():
            remaining = int(self._blocked_until - time.time())
            print(f"Session init skipped — still blocked ({remaining}s remaining).")
            return

        print("Initializing session...")
        self.session.cookies.clear()
        self._rotate_profile()
        self._apply_headers()

        try:
            self._throttle(MIN_API_DELAY)
            resp = self.session.get(VINTED_BASE_URL, timeout=15)
            self._request_count += 1
            self._last_request_time = time.time()

            if resp.status_code == 403:
                self._set_blocked()
                return

            resp.raise_for_status()
            self._api_call_count = 0
            self._update_vudt_header()
            print(f"Session initialized (got {len(self.session.cookies)} cookies)")
        except requests.RequestException as e:
            print(f"Session init warning: {e}")
            print("Will attempt API calls anyway...")

    def _maybe_refresh_session(self):
        self._api_call_count += 1
        if self._api_call_count >= SESSION_REFRESH_INTERVAL:
            if self._is_blocked():
                remaining = int(self._blocked_until - time.time())
                print(f"Session refresh skipped — still blocked ({remaining}s remaining).")
                return
            print(f"Refreshing session after {self._api_call_count} API calls...")
            self._init_session()

    # ── Search ────────────────────────────────────────────────────────────────

    def search(self, query: str, page: int = 1, catalog_ids: list = None,
               order: str = DEFAULT_ORDER, price_from: float = None,
               price_to: float = None, per_page: int = CATALOG_PER_PAGE,
               search_session_id: str = None) -> dict:
        """
        Search Vinted catalog for items (title/price/etc only — no description).

        Returns the raw API response as dict, or {} on failure.
        """
        if self._is_blocked():
            remaining = int(self._blocked_until - time.time())
            print(f"Search skipped — still blocked ({remaining}s remaining).")
            return {}

        url = f"{VINTED_API_URL}/catalog/items"
        if search_session_id is None:
            search_session_id = str(uuid.uuid4())
        params = [
            ("search_text", query),
            ("currency", DEFAULT_CURRENCY),
            ("order", order),
            ("page", page),
            ("per_page", per_page),
            ("search_session_id", search_session_id),
            ("screen_name", "catalog"),
            ("column_count", 2),
        ]

        for cid in (catalog_ids or []):
            params.append(("catalog_ids", cid))
        if price_from is not None:
            params.append(("price_from", price_from))
        if price_to is not None:
            params.append(("price_to", price_to))

        try:
            self._maybe_refresh_session()

            if self._is_blocked():
                remaining = int(self._blocked_until - time.time())
                print(f"Search skipped — blocked during session refresh ({remaining}s remaining).")
                return {}

            self._throttle(MIN_API_DELAY)
            resp = self.session.get(url, params=params, timeout=15)
            self._request_count += 1
            self._last_request_time = time.time()

            if resp.status_code == 403:
                print("403 Forbidden on search — entering block cooldown.")
                self._set_blocked()
                return {}

            if resp.status_code == 401:
                print("401 Unauthorized — refreshing session and retrying...")
                self._init_session()
                if self._is_blocked():
                    return {}
                self._throttle(MIN_API_DELAY)
                resp = self.session.get(url, params=params, timeout=15)
                self._request_count += 1
                self._last_request_time = time.time()

            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            print(f"Search request failed: {e}")
            if hasattr(e, "response") and e.response is not None:
                if e.response.status_code == 403:
                    self._set_blocked()
            return {}

    # ── Detail-page fetch (item description / user profile) ───────────────────

    def _get_with_retry(self, url: str, max_attempts: int = 3):
        """GET a detail page, retrying with backoff on 429.

        A batch of DETAIL_CONCURRENCY description/profile fetches can trip
        Vinted's per-endpoint rate limit even though it's well under the
        403-block threshold — that shows up as 429, not 403, and previously
        made the fetch (and the whole candidate item) silently give up.
        Respects `Retry-After` when Vinted sends one, otherwise backs off
        exponentially. Returns the Response (whatever its status), or None
        if still rate-limited after `max_attempts` tries.
        """
        for attempt in range(max_attempts):
            resp = self.session.get(url, timeout=15)
            self._request_count += 1
            if resp.status_code != 429:
                return resp
            if attempt == max_attempts - 1:
                return None
            retry_after = resp.headers.get("Retry-After")
            try:
                wait = float(retry_after)
            except (TypeError, ValueError):
                wait = 2 ** attempt
            wait += random.uniform(0.0, DETAIL_JITTER_SECONDS)
            print(f"429 Too Many Requests on {url} — retrying in {wait:.1f}s "
                  f"(attempt {attempt + 2}/{max_attempts})")
            time.sleep(wait)
        return None

    # ── Item description ─────────────────────────────────────────────────────

    def get_item_description(self, item_id: int, item_url: str) -> str | None:
        """
        Fetch an item's full description by scraping its public page.

        Vinted embeds a JSON-LD `Product` block in every item page with the
        full description text. Returns "" if the item has no description,
        or None if the page couldn't be fetched (blocked, removed, network
        error) — callers should treat None as "unknown", not "empty".

        Results are cached in-memory per scraper instance since a
        description doesn't change between requests within one search.

        Blocks (via a semaphore) so at most DETAIL_CONCURRENCY calls are
        in flight at once — call this from multiple threads to fetch a
        batch of descriptions in parallel instead of one at a time.
        """
        cached = self._get_cached_description(item_id)
        if cached is not None:
            return cached

        if self._is_blocked() or not item_url:
            return None

        with self._detail_semaphore:
            # Re-check after possibly waiting for a free slot.
            if self._is_blocked():
                return None
            try:
                time.sleep(random.uniform(0.0, DETAIL_JITTER_SECONDS))
                resp = self._get_with_retry(item_url)
                if resp is None:
                    print(f"Item page fetch failed for {item_id}: still rate-limited after retries")
                    return None

                if resp.status_code == 403:
                    print("403 Forbidden on item page — entering block cooldown.")
                    self._set_blocked()
                    return None
                if resp.status_code == 404:
                    self._cache_description(item_id, "")
                    return ""

                resp.raise_for_status()
                description = self._extract_description(resp.text) or ""
                self._cache_description(item_id, description)
                return description
            except requests.RequestException as e:
                print(f"Item page fetch failed for {item_id}: {e}")
                return None

    def get_item_descriptions(self, items: list[tuple[int, str]]) -> dict[int, str | None]:
        """Fetch descriptions for multiple (item_id, item_url) pairs concurrently."""
        results: dict[int, str | None] = {}
        if not items:
            return results

        with ThreadPoolExecutor(max_workers=DETAIL_CONCURRENCY) as pool:
            futures = {
                pool.submit(self.get_item_description, item_id, item_url): item_id
                for item_id, item_url in items
            }
            for future in futures:
                item_id = futures[future]
                results[item_id] = future.result()
        return results

    def _get_cached_description(self, item_id: int) -> str | None:
        with self._cache_lock:
            return self._description_cache.get(item_id)

    def _cache_description(self, item_id: int, description: str):
        with self._cache_lock:
            if len(self._description_cache) >= DESCRIPTION_CACHE_SIZE:
                self._description_cache.clear()
            self._description_cache[item_id] = description

    @staticmethod
    def _extract_description(html: str) -> str:
        """Pull `description` out of the item page's JSON-LD Product block."""
        idx = html.find(_LD_JSON_TAG)
        if idx == -1:
            return ""
        start = idx + len(_LD_JSON_TAG)
        end = html.find("</script>", start)
        if end == -1:
            return ""
        blob = html[start:end]
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            return ""
        return data.get("description", "") if isinstance(data, dict) else ""

    # ── Seller country ────────────────────────────────────────────────────────

    def get_user_country(self, user_id: int) -> dict | None:
        """
        Fetch a seller's country via their public profile.

        Returns {"title": "Czechy", "code": "CZ"}, or None if unavailable
        (blocked / removed / network error). Cached in-memory per scraper
        instance — a seller's country essentially never changes.

        Shares the same concurrency bound as `get_item_description` — call
        this from multiple threads for a batch instead of one at a time.
        """
        if not user_id:
            return None

        with self._cache_lock:
            if user_id in self._country_cache:
                return self._country_cache[user_id]

        if self._is_blocked():
            return None

        with self._detail_semaphore:
            if self._is_blocked():
                return None
            try:
                time.sleep(random.uniform(0.0, DETAIL_JITTER_SECONDS))
                resp = self._get_with_retry(f"{VINTED_API_URL}/users/{user_id}")
                if resp is None:
                    print(f"User profile fetch failed for {user_id}: still rate-limited after retries")
                    return None

                if resp.status_code == 403:
                    print("403 Forbidden on user profile — entering block cooldown.")
                    self._set_blocked()
                    return None
                if resp.status_code == 404:
                    self._cache_country(user_id, None)
                    return None

                resp.raise_for_status()
                data = resp.json().get("user") or {}
                title = data.get("country_title")
                code = data.get("country_iso_code")
                country = {"title": title, "code": code} if title or code else None
                self._cache_country(user_id, country)
                return country
            except requests.RequestException as e:
                print(f"User profile fetch failed for {user_id}: {e}")
                return None

    def get_user_countries(self, user_ids: list[int]) -> dict[int, dict | None]:
        """Fetch seller countries for multiple user ids concurrently."""
        results: dict[int, dict | None] = {}
        unique_ids = {uid for uid in user_ids if uid}
        if not unique_ids:
            return results

        with ThreadPoolExecutor(max_workers=DETAIL_CONCURRENCY) as pool:
            futures = {
                pool.submit(self.get_user_country, uid): uid
                for uid in unique_ids
            }
            for future in futures:
                uid = futures[future]
                results[uid] = future.result()
        return results

    def _cache_country(self, user_id: int, country: dict | None):
        with self._cache_lock:
            self._country_cache[user_id] = country


class ScraperPool:
    """Pools a direct `VintedScraper` identity with a proxy one (when
    VINTED_PROXY is configured) so requests get split across two source
    IPs instead of concentrating on one — each identity has its own
    cookies, device profile and throttle/block state, so a block on one
    doesn't stop the other from serving requests. With no proxy configured
    this is just a thin wrapper around a single direct identity.

    Exposes the same interface as `VintedScraper` (`search`,
    `get_item_descriptions`, `get_user_countries`).
    """

    def __init__(self, on_blocked=None):
        self._identities = [VintedScraper(on_blocked=on_blocked, use_proxy=False)]
        if VINTED_PROXY:
            self._identities.append(VintedScraper(on_blocked=on_blocked, use_proxy=True))
        self._rr_lock = threading.Lock()
        self._rr_index = 0

    def _next_identity(self) -> VintedScraper:
        """Round-robin across identities, preferring one that isn't
        currently in a block cooldown."""
        with self._rr_lock:
            order = [self._identities[(self._rr_index + i) % len(self._identities)]
                      for i in range(len(self._identities))]
            self._rr_index = (self._rr_index + 1) % len(self._identities)
        for identity in order:
            if not identity._is_blocked():
                return identity
        return order[0]

    def search(self, *args, **kwargs) -> dict:
        return self._next_identity().search(*args, **kwargs)

    def get_item_descriptions(self, items: list[tuple[int, str]]) -> dict[int, str | None]:
        return self._fan_out(items, lambda identity, batch: identity.get_item_descriptions(batch))

    def get_user_countries(self, user_ids: list[int]) -> dict[int, dict | None]:
        return self._fan_out(user_ids, lambda identity, batch: identity.get_user_countries(batch))

    def total_request_count(self) -> int:
        """Sum of every HTTP request sent to Vinted across all identities so
        far (lifetime, not per-search — callers wanting a per-search count
        should snapshot this before and after)."""
        return sum(identity.request_count for identity in self._identities)

    def _fan_out(self, work_items: list, call) -> dict:
        """Split a batch roughly evenly across identities and run each
        identity's share concurrently — a big batch spends half its
        requests on each IP instead of all of them on one."""
        if not work_items:
            return {}
        if len(self._identities) == 1:
            return call(self._identities[0], work_items)

        buckets = [[] for _ in self._identities]
        for i, item in enumerate(work_items):
            buckets[i % len(self._identities)].append(item)

        results = {}
        with ThreadPoolExecutor(max_workers=len(self._identities)) as pool:
            futures = [pool.submit(call, identity, batch)
                       for identity, batch in zip(self._identities, buckets) if batch]
            for future in futures:
                results.update(future.result())
        return results


def main():
    parser = argparse.ArgumentParser(description="Search Vinted and print matching titles")
    parser.add_argument("query", nargs="?", default="", help="Search query")
    parser.add_argument("--catalog", type=int, default=None, help="Category ID (e.g. 2994 for Elektronika)")
    parser.add_argument("--price-from", type=float, default=None)
    parser.add_argument("--price-to", type=float, default=None)
    args = parser.parse_args()

    scraper = VintedScraper()
    data = scraper.search(
        args.query,
        catalog_ids=[args.catalog] if args.catalog else None,
        price_from=args.price_from,
        price_to=args.price_to,
    )
    items = data.get("items", [])
    print(f"\nFound {len(items)} item(s):\n")
    for item in items:
        price = item.get("price") or {}
        print(f"  [{item.get('id')}] {item.get('title')} — {price.get('amount')} {price.get('currency_code')}")
        print(f"      {item.get('url')}")


if __name__ == "__main__":
    main()
