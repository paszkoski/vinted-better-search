#!/usr/bin/env python3
"""
Vinted Scraper — search Vinted's catalog and fetch per-item descriptions.

Vinted's own `catalog/items` API only returns a title for each item — no
description — so its search silently ignores whatever the description
contains. `get_item_descriptions()` fetches each public item page and pulls
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
    DETAIL_BATCH_SIZE,
    DETAIL_BATCH_COOLDOWN_SECONDS,
    DETAIL_MAX_ROUNDS,
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
        # Counts detail-fetch dispatches toward DETAIL_BATCH_SIZE; every
        # thread pauses together once the batch fills up (see
        # _stagger_detail_dispatch).
        self._detail_batch_lock = threading.Lock()
        self._detail_batch_count = 0
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
            self._close_stale_connections()
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

    def _close_stale_connections(self):
        """Discard pooled keep-alive connections after a VPN rotation — they
        were opened over the old egress IP and are now dead. Without this,
        a request can pull another already-dead connection back out of the
        pool instead of opening a fresh one, causing repeated
        RemoteDisconnected errors across several retries even though the
        rotation itself succeeded."""
        try:
            self.session.close()
        except Exception as e:
            print(f"Closing stale connections failed: {e}")

    # ── Request throttling ────────────────────────────────────────────────────

    def _throttle(self, min_delay: float):
        elapsed = time.time() - self._last_request_time
        if elapsed < min_delay:
            wait = min_delay - elapsed + random.uniform(0.0, 0.4)
            time.sleep(wait)

    def _stagger_detail_dispatch(self):
        """Space out detail-fetch dispatches by at least DETAIL_MIN_GAP_SECONDS,
        on top of the existing random jitter, and pause every DETAIL_BATCH_SIZE
        dispatches for DETAIL_BATCH_COOLDOWN_SECONDS.

        DETAIL_CONCURRENCY lets several detail fetches run *in flight* at
        once, but without the gap/jitter, all of them fire within the same
        instant — a burst that looks automated on its own. Separately,
        Vinted's 429 limit on these fetches is a hard per-window request-count
        threshold (empirically right around DETAIL_BATCH_SIZE) rather than
        burst detection, so once the batch fills up every thread pauses
        together — under the same lock, so nothing else dispatches mid-pause —
        before the next batch starts.
        """
        with self._detail_batch_lock:
            self._detail_batch_count += 1
            if self._detail_batch_count > DETAIL_BATCH_SIZE:
                self._detail_batch_count = 1
                print(f"Reached {DETAIL_BATCH_SIZE}-request detail-fetch batch — "
                      f"pausing {DETAIL_BATCH_COOLDOWN_SECONDS:.0f}s before continuing")
                time.sleep(DETAIL_BATCH_COOLDOWN_SECONDS)

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

    def _run_in_rounds(self, keyed_items: list[tuple], fetch_one) -> dict:
        """Run fetch_one(key, payload) concurrently for each (key, payload) in
        keyed_items. Whenever fetch_one reports it needs a retry (blocked,
        rate-limited, or errored — anything transient), that item is deferred
        to a later round instead of retried in place: the rest of the batch
        keeps moving instead of stalling behind one stuck item, and by the
        time a deferred item comes back around, a block/rate-limit has often
        cleared (VPN rotations and batch cooldowns triggered by other items'
        attempts happen in the meantime too). Runs up to DETAIL_MAX_ROUNDS
        rounds; anything still pending after that is recorded as None.

        fetch_one(key, payload) -> (value, needs_retry).
        """
        results = {}
        pending = keyed_items
        for round_num in range(1, DETAIL_MAX_ROUNDS + 1):
            if not pending:
                break
            if round_num > 1:
                print(f"Retrying {len(pending)} deferred item(s) — round {round_num}/{DETAIL_MAX_ROUNDS}")
            retry_needed = []
            with ThreadPoolExecutor(max_workers=DETAIL_CONCURRENCY) as pool:
                futures = {pool.submit(fetch_one, key, payload): (key, payload) for key, payload in pending}
                for future in futures:
                    key, payload = futures[future]
                    value, needs_retry = future.result()
                    if needs_retry:
                        retry_needed.append((key, payload))
                    else:
                        results[key] = value
            pending = retry_needed

        for key, _ in pending:
            self._failed_count += 1
            results[key] = None
        return results

    # ── Item description ─────────────────────────────────────────────────────

    def _fetch_description_attempt(self, item_id: int, item_url: str) -> tuple[str | None, bool]:
        """One attempt at an item's description. Returns (description, needs_retry):
        description is "" if the item has no description, None if unresolved
        (cache miss and no successful attempt yet) — callers should treat
        None as "unknown", not "empty". needs_retry True means this item
        should be deferred to a later round (see _run_in_rounds)."""
        cached = self._get_cached_description(item_id)
        if cached is not None:
            return cached, False
        if not item_url:
            return None, False
        if self._is_blocked():
            return None, True

        with self._detail_semaphore:
            if self._is_blocked():
                return None, True
            self._stagger_detail_dispatch()
            try:
                resp = self.session.get(item_url, timeout=15)
            except requests.RequestException as e:
                print(f"Item page fetch error for {item_id}: {e} — deferring")
                return None, True
            self._request_count += 1

            if resp.status_code == 429:
                print(f"429 Too Many Requests on item {item_id} — deferring")
                return None, True
            if resp.status_code == 403:
                print(f"403 Forbidden on item {item_id} — rotating VPN, deferring")
                self._set_blocked()
                if vpn.rotate(reason=f"403 on {item_url}"):
                    self._blocked_until = 0.0
                    self._close_stale_connections()
                return None, True
            if resp.status_code == 404:
                self._cache_description(item_id, "")
                return "", False

            try:
                resp.raise_for_status()
            except requests.RequestException as e:
                print(f"Item page fetch failed for {item_id}: {e} — deferring")
                return None, True

            description = self._extract_description(resp.text) or ""
            self._cache_description(item_id, description)
            return description, False

    def get_item_descriptions(self, items: list[tuple[int, str]]) -> dict[int, str | None]:
        """Fetch descriptions for multiple (item_id, item_url) pairs
        concurrently, deferring any that hit a block/rate-limit/error to
        later rounds instead of one at a time (see _run_in_rounds)."""
        if not items:
            return {}
        keyed = [(item_id, item_url) for item_id, item_url in items]
        return self._run_in_rounds(keyed, self._fetch_description_attempt)

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

    def _fetch_country_attempt(self, user_id: int, _payload=None) -> tuple[dict | None, bool]:
        """One attempt at a seller's country via their public profile.
        Returns (country, needs_retry): country is {"title": ..., "code": ...}
        or None if unavailable (removed / no country on file). needs_retry
        True means this item should be deferred to a later round (see
        _run_in_rounds)."""
        with self._cache_lock:
            if user_id in self._country_cache:
                return self._country_cache[user_id], False

        if self._is_blocked():
            return None, True

        with self._detail_semaphore:
            if self._is_blocked():
                return None, True
            self._stagger_detail_dispatch()
            url = f"{VINTED_API_URL}/users/{user_id}"
            try:
                resp = self.session.get(url, timeout=15)
            except requests.RequestException as e:
                print(f"User profile fetch error for {user_id}: {e} — deferring")
                return None, True
            self._request_count += 1

            if resp.status_code == 429:
                print(f"429 Too Many Requests on user {user_id} — deferring")
                return None, True
            if resp.status_code == 403:
                print(f"403 Forbidden on user {user_id} — rotating VPN, deferring")
                self._set_blocked()
                if vpn.rotate(reason=f"403 on {url}"):
                    self._blocked_until = 0.0
                    self._close_stale_connections()
                return None, True
            if resp.status_code == 404:
                self._cache_country(user_id, None)
                return None, False

            try:
                resp.raise_for_status()
            except requests.RequestException as e:
                print(f"User profile fetch failed for {user_id}: {e} — deferring")
                return None, True

            data = resp.json().get("user") or {}
            title = data.get("country_title")
            code = data.get("country_iso_code")
            country = {"title": title, "code": code} if title or code else None
            self._cache_country(user_id, country)
            return country, False

    def get_user_countries(self, user_ids: list[int]) -> dict[int, dict | None]:
        """Fetch seller countries for multiple user ids concurrently,
        deferring any that hit a block/rate-limit/error to later rounds
        instead of one at a time (see _run_in_rounds)."""
        unique_ids = {uid for uid in user_ids if uid}
        if not unique_ids:
            return {}
        keyed = [(uid, None) for uid in unique_ids]
        return self._run_in_rounds(keyed, self._fetch_country_attempt)

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
