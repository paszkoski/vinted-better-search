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

import vpn
from config import (
    VINTED_BASE_URL,
    VINTED_API_URL,
    VINTED_CATALOG_URL,
    DEFAULT_CURRENCY,
    DEFAULT_ORDER,
    CATALOG_PER_PAGE,
    SESSION_REFRESH_INTERVAL,
    BLOCK_WAIT_SECONDS,
    MIN_API_DELAY,
    DETAIL_CONCURRENCY,
    DETAIL_JITTER_SECONDS,
    DETAIL_MIN_GAP_SECONDS,
    DESCRIPTION_CACHE_SIZE,
    VPN_MAX_ROTATE_ATTEMPTS,
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

    A single identity — its own cookies, device profile and throttle/block
    state. On a 403 block, rotates to a new NordVPN server (see vpn.py) and
    retries the request once instead of just cooling down.
    """

    def __init__(self, on_blocked=None):
        self.session = requests.Session()
        self._api_call_count = 0
        self._request_count = 0  # every HTTP request actually sent to Vinted (incl. retries)
        self._retry_count = 0    # 429 retries attempted
        self._failed_count = 0   # detail fetches that gave up (rate-limited, blocked, network error)
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
        # In-memory description cache: item_id -> description (or "" if none/unavailable)
        self._description_cache: dict[int, str] = {}
        # In-memory seller-country cache: user_id -> {"title": ..., "code": ...} or None
        self._country_cache: dict[int, dict | None] = {}
        self._cache_lock = threading.Lock()
        self._block_lock = threading.Lock()
        # Bounds how many item-description / user-profile fetches run in flight at once
        self._detail_semaphore = threading.Semaphore(DETAIL_CONCURRENCY)
        # Staggers detail-fetch *dispatches* so DETAIL_CONCURRENCY workers don't
        # all fire in the same instant — a burst like that trips Vinted's rate
        # limit almost immediately (seen escalating straight to a 403 block).
        self._detail_throttle_lock = threading.Lock()
        self._last_detail_dispatch = 0.0
        self._apply_headers()
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

    @property
    def retry_count(self) -> int:
        return self._retry_count

    @property
    def failed_count(self) -> int:
        return self._failed_count

    # ── Block detection / cooldown ────────────────────────────────────────────

    def _is_blocked(self) -> bool:
        return time.time() < self._blocked_until

    def _set_blocked(self):
        with self._block_lock:
            self._blocked_until = time.time() + BLOCK_WAIT_SECONDS
            wait_min = BLOCK_WAIT_SECONDS / 60
            print(f"Blocked by Vinted (403)! Pausing for {wait_min:.1f} minutes unless a VPN rotation succeeds...")
            if self._on_blocked:
                try:
                    self._on_blocked(wait_min)
                except Exception as e:
                    print(f"Block notification error: {e}")

    def _rotate_and_retry_get(self, url: str, **kwargs) -> requests.Response | None:
        """Called right after a GET came back 403. Rotates to a new NordVPN
        server and retries — if that retry is 403 too, rotates again and
        retries again, up to VPN_MAX_ROTATE_ATTEMPTS times, so one blocked
        server doesn't stall the rest of a search. Returns the first
        non-403 Response, or None if rotation itself failed, the retry
        errored, or every attempt was still 403 — callers should treat None
        the same as still being blocked (the BLOCK_WAIT_SECONDS cooldown
        `_set_blocked()` already set stands)."""
        for attempt in range(1, VPN_MAX_ROTATE_ATTEMPTS + 1):
            if not vpn.rotate(reason=f"403 on {url} (attempt {attempt}/{VPN_MAX_ROTATE_ATTEMPTS})"):
                return None
            self._blocked_until = 0.0  # rotation succeeded — lift this identity's cooldown
            self._throttle(MIN_API_DELAY)
            try:
                resp = self.session.get(url, timeout=15, **kwargs)
            except requests.RequestException as e:
                # The session's pooled keep-alive connections were opened over
                # the old IP — the first request after a rotation can find one
                # already dead rather than getting a clean HTTP response. Treat
                # that the same as a 403: rotate again and keep trying, rather
                # than giving up on the item outright.
                print(f"Request error after rotating (attempt {attempt}/{VPN_MAX_ROTATE_ATTEMPTS}) on {url}: {e}")
                continue
            self._request_count += 1
            self._last_request_time = time.time()
            if resp.status_code != 403:
                return resp
            print(f"Still 403 after rotating (attempt {attempt}/{VPN_MAX_ROTATE_ATTEMPTS}) on {url}")
        return None

    # ── Request throttling ────────────────────────────────────────────────────

    def _throttle(self, min_delay: float):
        elapsed = time.time() - self._last_request_time
        if elapsed < min_delay:
            wait = min_delay - elapsed + random.uniform(0.0, 0.4)
            time.sleep(wait)

    def _stagger_detail_dispatch(self):
        """Space out detail-fetch dispatches by at least DETAIL_MIN_GAP_SECONDS,
        on top of the existing random jitter.

        DETAIL_CONCURRENCY lets several detail fetches run *in flight* at
        once, but without this, all of them fire within the same
        DETAIL_JITTER_SECONDS window — a burst that Vinted's rate limit
        reads as automated and escalates to a 403 block almost immediately.
        Reserving each dispatch's earliest start time under a lock keeps
        the concurrency (they still overlap in flight) while guaranteeing
        real spacing between when each one actually goes out.
        """
        with self._detail_throttle_lock:
            now = time.time()
            earliest = max(now, self._last_detail_dispatch + DETAIL_MIN_GAP_SECONDS)
            self._last_detail_dispatch = earliest
        wait = (earliest - now) + random.uniform(0.0, DETAIL_JITTER_SECONDS)
        if wait > 0:
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
                retried = self._rotate_and_retry_get(VINTED_BASE_URL)
                if retried is None or retried.status_code == 403:
                    if retried is not None:
                        self._set_blocked()
                    return
                resp = retried

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

        url = f"{VINTED_CATALOG_URL}/items"
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
                print("403 Forbidden on search — rotating VPN and retrying.")
                self._set_blocked()
                retried = self._rotate_and_retry_get(url, params=params)
                if retried is None or retried.status_code == 403:
                    if retried is not None:
                        self._set_blocked()
                    return {}
                resp = retried

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
            if hasattr(e, "response") and e.response is not None and e.response.status_code == 403:
                self._set_blocked()
                retried = self._rotate_and_retry_get(url, params=params)
                if retried is None or retried.status_code == 403:
                    if retried is not None:
                        self._set_blocked()
                    return {}
                try:
                    retried.raise_for_status()
                    return retried.json()
                except requests.RequestException:
                    pass
            return {}

    # ── Detail-page fetch (item description / user profile) ───────────────────

    def _get_with_retry(self, url: str, max_attempts: int = 10):
        """GET a detail page, retrying with backoff on 429.

        A batch of DETAIL_CONCURRENCY description/profile fetches can trip
        Vinted's per-endpoint rate limit even though it's well under the
        403-block threshold — that shows up as 429, not 403, and previously
        made the fetch (and the whole candidate item) silently give up.
        Also rotates to a new VPN server before each retry — Vinted's rate
        limit reads as tied to the source IP, so a new one plus the backoff
        wait clears it faster than waiting alone. Respects `Retry-After`
        when Vinted sends one, otherwise backs off exponentially (capped).
        Returns the Response (whatever its status), or None if still
        rate-limited after `max_attempts` tries.
        """
        for attempt in range(max_attempts):
            try:
                resp = self.session.get(url, timeout=15)
            except requests.RequestException as e:
                # A pooled keep-alive connection opened before a VPN rotation
                # can come back dead rather than give a clean response — treat
                # that as transient and retry rather than failing the item.
                if attempt == max_attempts - 1:
                    self._failed_count += 1
                    print(f"Request error on {url} after {max_attempts} attempts: {e}")
                    return None
                self._retry_count += 1
                wait = min(2 ** attempt, 20) + random.uniform(0.0, DETAIL_JITTER_SECONDS)
                print(f"Request error on {url} ({e}) — retrying in {wait:.1f}s "
                      f"(attempt {attempt + 2}/{max_attempts})")
                time.sleep(wait)
                continue
            self._request_count += 1
            if resp.status_code != 429:
                return resp
            if attempt == max_attempts - 1:
                self._failed_count += 1
                return None
            self._retry_count += 1
            vpn.rotate(reason=f"429 on {url} (attempt {attempt + 1}/{max_attempts})")
            retry_after = resp.headers.get("Retry-After")
            try:
                wait = float(retry_after)
            except (TypeError, ValueError):
                wait = min(2 ** attempt, 20)
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
                self._stagger_detail_dispatch()
                resp = self._get_with_retry(item_url)
                if resp is None:
                    print(f"Item page fetch failed for {item_id}: still rate-limited after retries")
                    return None

                if resp.status_code == 403:
                    print("403 Forbidden on item page — rotating VPN and retrying.")
                    self._set_blocked()
                    retried = self._rotate_and_retry_get(item_url)
                    if retried is None or retried.status_code == 403:
                        if retried is not None:
                            self._set_blocked()
                        self._failed_count += 1
                        return None
                    resp = retried
                if resp.status_code == 404:
                    self._cache_description(item_id, "")
                    return ""

                resp.raise_for_status()
                description = self._extract_description(resp.text) or ""
                self._cache_description(item_id, description)
                return description
            except requests.RequestException as e:
                print(f"Item page fetch failed for {item_id}: {e}")
                self._failed_count += 1
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
                self._stagger_detail_dispatch()
                resp = self._get_with_retry(f"{VINTED_API_URL}/users/{user_id}")
                if resp is None:
                    print(f"User profile fetch failed for {user_id}: still rate-limited after retries")
                    return None

                if resp.status_code == 403:
                    print("403 Forbidden on user profile — rotating VPN and retrying.")
                    self._set_blocked()
                    retried = self._rotate_and_retry_get(f"{VINTED_API_URL}/users/{user_id}")
                    if retried is None or retried.status_code == 403:
                        if retried is not None:
                            self._set_blocked()
                        self._failed_count += 1
                        return None
                    resp = retried
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
                self._failed_count += 1
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
