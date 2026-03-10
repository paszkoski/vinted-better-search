#!/usr/bin/env python3
"""
Vinted Scraper — Search and monitor items on Vinted.

Usage:
    python scraper.py                                   # Search with default query
    python scraper.py "xreal beam pro"                   # Search with custom query
    python scraper.py "xreal beam pro" --catalog 2994    # Filter by category
    python scraper.py --monitor                          # Monitor mode (polls every 5 min)
    python scraper.py --monitor --catalog 2994           # Monitor with category filter
"""

import sys
import json
import time
import uuid
import random
import secrets
import argparse
import requests
from datetime import datetime

from config import (
    VINTED_BASE_URL,
    VINTED_API_URL,
    DEFAULT_SEARCH_QUERY,
    DEFAULT_CURRENCY,
    DEFAULT_ORDER,
    DEFAULT_PER_PAGE,
    DEFAULT_CATALOG_ID,
    DEFAULT_PRICE_FROM,
    DEFAULT_PRICE_TO,
    STATE_FILE,
    POLL_INTERVAL,
    SESSION_REFRESH_INTERVAL,
    BLOCK_WAIT_SECONDS,
    MIN_API_DELAY,
    NOTIFY_N8N_ENABLED,
    N8N_WEBHOOK_URL,
)

# ── Mobile app profiles (from real iOS Vinted app traffic) ────────────────────
# User-Agent format: vinted-ios Vinted/<version> (<bundle>; build:<build>; iOS <ios>) <model>
_APP_VERSION = "26.6.1"
_BUILD = "30214"

MOBILE_PROFILES = [
    {
        "User-Agent": f"vinted-ios Vinted/{_APP_VERSION} (lt.manodrabuziai.pl; build:{_BUILD}; iOS 17.2.1) iPhone16,1",
        "X-Device-Model": "iPhone16,1",
    },
    {
        "User-Agent": f"vinted-ios Vinted/{_APP_VERSION} (lt.manodrabuziai.pl; build:{_BUILD}; iOS 17.1.2) iPhone15,2",
        "X-Device-Model": "iPhone15,2",
    },
    {
        "User-Agent": f"vinted-ios Vinted/{_APP_VERSION} (lt.manodrabuziai.pl; build:{_BUILD}; iOS 16.7.4) iPhone14,2",
        "X-Device-Model": "iPhone14,2",
    },
    {
        "User-Agent": f"vinted-ios Vinted/{_APP_VERSION} (lt.manodrabuziai.pl; build:{_BUILD}; iOS 18.1.0) iPhone17,1",
        "X-Device-Model": "iPhone17,1",
    },
]


class VintedScraper:
    """Scraper for Vinted catalog items."""

    def __init__(self, on_blocked=None):
        self.session = requests.Session()
        self._api_call_count = 0
        self._blocked_until = 0.0
        self._last_request_time = 0.0
        # Stable per-instance identifiers (simulate a real device)
        self._device_uuid = secrets.token_hex(16)
        self._anon_id = str(uuid.uuid4())
        self._profile = random.choice(MOBILE_PROFILES)
        # Optional callback: on_blocked(wait_minutes: int) — called when a 403 block is detected
        self._on_blocked = on_blocked
        self._apply_headers()
        self._init_session()

    # ── Header / profile management ───────────────────────────────────────────

    def _rotate_profile(self):
        """Pick a new random mobile profile on each session refresh."""
        self._profile = random.choice(MOBILE_PROFILES)

    def _apply_headers(self):
        """Apply current mobile-app headers to the session."""
        self.session.headers.clear()
        self.session.headers.update({
            **self._profile,
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "pl-PL",
            "Connection": "keep-alive",
            "Locale": "pl-PL",
            "Short-Bundle-Version": _APP_VERSION,
            "X-App-Version": _APP_VERSION,
            "X-Anon-Id": self._anon_id,
            "X-Device-UUID": self._device_uuid,
            "X-Session-Id": str(uuid.uuid4()),  # fresh per session
        })

    # ── Block detection / cooldown ────────────────────────────────────────────

    def _is_blocked(self) -> bool:
        """Return True if currently in a 403 cooldown period."""
        return time.time() < self._blocked_until

    def _set_blocked(self):
        """Activate block cooldown, log how long we'll wait, and fire the callback."""
        self._blocked_until = time.time() + BLOCK_WAIT_SECONDS
        wait_min = BLOCK_WAIT_SECONDS // 60
        print(f"🚫 Blocked by Vinted (403)! Pausing all requests for {wait_min} minutes "
              f"(until {datetime.fromtimestamp(self._blocked_until).strftime('%H:%M:%S')})...")
        if self._on_blocked:
            try:
                self._on_blocked(wait_min)
            except Exception as e:
                print(f"⚠️  Block notification error: {e}")

    # ── Request throttling ────────────────────────────────────────────────────

    def _throttle(self):
        """Sleep so that at least MIN_API_DELAY seconds pass between requests."""
        elapsed = time.time() - self._last_request_time
        if elapsed < MIN_API_DELAY:
            wait = MIN_API_DELAY - elapsed + random.uniform(0.0, 0.5)
            time.sleep(wait)

    # ── Session management ────────────────────────────────────────────────────

    def _init_session(self):
        """Visit Vinted homepage to obtain session cookies."""
        if self._is_blocked():
            remaining = int(self._blocked_until - time.time())
            print(f"⏸️  Session init skipped — still blocked ({remaining}s remaining).")
            return

        print("🔄 Initializing session...")
        self.session.cookies.clear()
        self._rotate_profile()
        self._apply_headers()

        try:
            self._throttle()
            resp = self.session.get(VINTED_BASE_URL, timeout=15)
            self._last_request_time = time.time()

            if resp.status_code == 403:
                self._set_blocked()
                return

            resp.raise_for_status()
            self._api_call_count = 0
            print(f"✅ Session initialized (got {len(self.session.cookies)} cookies)")
        except requests.RequestException as e:
            print(f"⚠️  Session init warning: {e}")
            print("   Will attempt API calls anyway...")

    def _maybe_refresh_session(self):
        """Refresh session cookies every SESSION_REFRESH_INTERVAL API calls."""
        self._api_call_count += 1
        if self._api_call_count >= SESSION_REFRESH_INTERVAL:
            if self._is_blocked():
                remaining = int(self._blocked_until - time.time())
                print(f"⏸️  Session refresh skipped — still blocked ({remaining}s remaining).")
                return
            print(f"🔁 Refreshing session after {self._api_call_count} API calls...")
            self._init_session()

    # ── Search ────────────────────────────────────────────────────────────────

    def search(self, query: str, page: int = 1, catalog_id: int = None,
               order: str = DEFAULT_ORDER, price_from: float = None,
               price_to: float = None, brand_ids: list = None,
               size_ids: list = None, color_ids: list = None,
               material_ids: list = None, status_ids: list = None) -> dict:
        """
        Search Vinted catalog for items.

        Args:
            query: Search text
            page: Page number (1-indexed)
            catalog_id: Optional category ID (e.g. 2994 for Elektronika)
            order: Sort order ('relevance' or 'newest_first')
            price_from: Minimum price filter
            price_to: Maximum price filter
            brand_ids: List of brand IDs to filter by
            size_ids: List of size IDs to filter by
            color_ids: List of color IDs to filter by
            material_ids: List of material IDs to filter by
            status_ids: List of item condition/status IDs to filter by

        Returns:
            API response as dict, or empty dict on failure
        """
        if self._is_blocked():
            remaining = int(self._blocked_until - time.time())
            print(f"⏸️  Search skipped — still blocked ({remaining}s remaining).")
            return {}

        url = f"{VINTED_API_URL}/catalog/items"
        # Use list of tuples to support repeated keys (e.g. brand_ids[]=1&brand_ids[]=2)
        params = [
            ("search_text", query),
            ("currency", DEFAULT_CURRENCY),
            ("order", order),
            ("page", page),
            ("per_page", DEFAULT_PER_PAGE),
        ]

        if catalog_id is not None:
            params.append(("catalog_ids", catalog_id))
        if price_from is not None:
            params.append(("price_from", price_from))
        if price_to is not None:
            params.append(("price_to", price_to))
        for bid in (brand_ids or []):
            params.append(("brand_ids[]", bid))
        for sid in (size_ids or []):
            params.append(("size_ids[]", sid))
        for cid in (color_ids or []):
            params.append(("color_ids[]", cid))
        for mid in (material_ids or []):
            params.append(("material_ids[]", mid))
        for stid in (status_ids or []):
            params.append(("status_ids[]", stid))

        extras = []
        if catalog_id:
            extras.append(f"catalog: {catalog_id}")
        if price_from is not None or price_to is not None:
            price_range = f"{price_from or '∞'}–{price_to or '∞'}"
            extras.append(f"price: {price_range}")
        if brand_ids:
            extras.append(f"brands: {brand_ids}")
        if size_ids:
            extras.append(f"sizes: {size_ids}")
        if color_ids:
            extras.append(f"colors: {color_ids}")
        if material_ids:
            extras.append(f"materials: {material_ids}")
        if status_ids:
            extras.append(f"conditions: {status_ids}")
        extra_str = f", {', '.join(extras)}" if extras else ""
        print(f"🔍 Searching for: \"{query}\" (page {page}{extra_str}, order: {order})...")

        try:
            self._maybe_refresh_session()

            # Bail out if _maybe_refresh_session triggered a block
            if self._is_blocked():
                remaining = int(self._blocked_until - time.time())
                print(f"⏸️  Search skipped — blocked during session refresh ({remaining}s remaining).")
                return {}

            self._throttle()
            resp = self.session.get(url, params=params, timeout=15)
            self._last_request_time = time.time()

            if resp.status_code == 403:
                print("🚫 403 Forbidden on search — entering block cooldown.")
                self._set_blocked()
                return {}

            if resp.status_code == 401:
                print("🔒 401 Unauthorized — refreshing session and retrying...")
                self._init_session()
                if self._is_blocked():
                    return {}
                self._throttle()
                resp = self.session.get(url, params=params, timeout=15)
                self._last_request_time = time.time()

            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            print(f"❌ Search request failed: {e}")
            if hasattr(e, "response") and e.response is not None:
                status = e.response.status_code
                print(f"   Status: {status}")
                print(f"   Body: {e.response.text[:500]}")
                if status == 403:
                    self._set_blocked()
            return {}

    def display_items(self, items: list, header: str = None):
        """Display a list of items in a readable format."""
        if not items:
            print("\n😕 No items found.")
            return

        if header:
            print(f"\n{'=' * 80}")
            print(f"  {header}")
            print(f"{'=' * 80}\n")

        for i, item in enumerate(items, 1):
            item_id = item.get("id", "?")
            title = item.get("title", "No title")
            price_info = item.get("price", {})
            price = f"{price_info.get('amount', '?')} {price_info.get('currency_code', '')}"
            total_price_info = item.get("total_item_price", {})
            total_price = (
                f"{total_price_info.get('amount', '?')} {total_price_info.get('currency_code', '')}"
                if total_price_info
                else "N/A"
            )
            url = item.get("url", "")
            status = item.get("status", "")
            brand = item.get("brand_title", "")
            favs = item.get("favourite_count", 0)

            print(f"  [{i}] {title}")
            print(f"      💰 Price: {price}  (Total incl. fees: {total_price})")
            if brand:
                print(f"      🏷️  Brand: {brand}")
            if status:
                print(f"      📦 Status: {status}")
            print(f"      ❤️  Favourites: {favs}")
            print(f"      🆔 ID: {item_id}")
            print(f"      🔗 {url}")
            print()

    def display_results(self, data: dict):
        """Display search results with pagination info."""
        items = data.get("items", [])
        pagination = data.get("pagination", {})
        total = pagination.get("total_entries", 0)
        current_page = pagination.get("current_page", 1)
        total_pages = pagination.get("total_pages", 1)
        self.display_items(items, f"Found {total} item(s)  —  Page {current_page}/{total_pages}")

    # ── State management ──────────────────────────────────────────────

    @staticmethod
    def _load_state() -> dict:
        """Load monitor state from JSON file."""
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _save_state(state: dict):
        """Save monitor state to JSON file."""
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)

    @staticmethod
    def _state_key(query: str, catalog_id: int = None,
                   price_from: float = None, price_to: float = None) -> str:
        """Generate a unique state key for a query + filters combo."""
        key = query.lower().strip()
        if catalog_id is not None:
            key += f"__cat{catalog_id}"
        if price_from is not None:
            key += f"__pf{price_from}"
        if price_to is not None:
            key += f"__pt{price_to}"
        return key

    # ── Monitor mode ──────────────────────────────────────────────────

    def check_new_items(self, query: str, catalog_id: int = None,
                        price_from: float = None, price_to: float = None) -> list:
        """
        Fetch items sorted by newest_first, return only items newer than last check.

        First run (no last_seen_id): fetches only page 1 to establish baseline.
        Subsequent runs: paginates through results until hitting the last seen
        item ID or running out of pages.
        """
        state = self._load_state()
        key = self._state_key(query, catalog_id, price_from=price_from, price_to=price_to)
        last_seen_id = state.get(key, {}).get("last_seen_id")
        is_first_run = last_seen_id is None

        new_items = []
        page = 1
        newest_id_this_run = None

        while True:
            data = self.search(query, page=page, catalog_id=catalog_id,
                               order="newest_first", price_from=price_from,
                               price_to=price_to)
            items = data.get("items", [])
            pagination = data.get("pagination", {})
            total_pages = pagination.get("total_pages", 1)

            if not items:
                break

            # Track the absolute newest item (first item on first page)
            if newest_id_this_run is None and items:
                newest_id_this_run = items[0].get("id")

            # First run: just grab page 1 to record the newest ID — no need to paginate
            if is_first_run:
                new_items.extend(items)
                break

            # Subsequent runs: collect items until we hit the last seen ID
            found_old = False
            for item in items:
                item_id = item.get("id")
                if item_id == last_seen_id:
                    found_old = True
                    break
                new_items.append(item)

            if found_old or page >= total_pages:
                break

            # Small random delay between paginated requests
            delay = random.uniform(1.0, 3.0)
            print(f"   ⏳ Waiting {delay:.1f}s before next page...")
            time.sleep(delay)
            page += 1

        # Update state with the newest item ID
        if newest_id_this_run is not None:
            state[key] = {
                "last_seen_id": newest_id_this_run,
                "last_checked": datetime.now().isoformat(),
                "query": query,
                "catalog_id": catalog_id,
                "price_from": price_from,
                "price_to": price_to,
            }
            self._save_state(state)

        return new_items

    def monitor(self, query: str, catalog_id: int = None, interval: int = POLL_INTERVAL,
                price_from: float = None, price_to: float = None):
        """
        Continuously monitor for new items, polling at the given interval.

        First run: shows all current items and saves state.
        Subsequent runs: shows only new items since last check.
        """
        state = self._load_state()
        key = self._state_key(query, catalog_id, price_from=price_from, price_to=price_to)
        is_first_run = key not in state

        extras = []
        if catalog_id:
            extras.append(f"catalog: {catalog_id}")
        if price_from is not None or price_to is not None:
            extras.append(f"price: {price_from or '∞'}–{price_to or '∞'}")
        extra_str = f" ({', '.join(extras)})" if extras else ""
        print(f"\n👁️  Monitor mode: \"{query}\"{extra_str}")
        print(f"   Polling every {interval} seconds. Press Ctrl+C to stop.\n")

        if is_first_run:
            print("📋 First run — fetching current items to establish baseline...\n")

        try:
            while True:
                now = datetime.now().strftime("%H:%M:%S")
                print(f"⏰ [{now}] Checking for new items...")

                new_items = self.check_new_items(query, catalog_id,
                                                price_from=price_from, price_to=price_to)

                if is_first_run:
                    self.display_items(
                        new_items,
                        f"Baseline: {len(new_items)} current item(s) — will notify on new ones"
                    )
                    is_first_run = False
                elif new_items:
                    self.display_items(
                        new_items,
                        f"🆕 {len(new_items)} NEW item(s) found!"
                    )
                    self.notify_new_items(new_items, query)
                else:
                    print("   No new items.\n")

                # Add ±20% jitter to the interval
                jitter = random.uniform(-0.2, 0.2) * interval
                actual_sleep = max(10, interval + jitter)
                print(f"💤 Sleeping {actual_sleep:.0f}s until next check...\n")
                time.sleep(actual_sleep)

        except KeyboardInterrupt:
            print("\n\n👋 Monitor stopped.")

    # ── Notifications ─────────────────────────────────────────────────

    def _notify_n8n(self, item: dict, query: str):
        """Send a single item to the configured n8n webhook as JSON."""
        if not N8N_WEBHOOK_URL:
            print("   ⚠️  n8n notification enabled but N8N_WEBHOOK_URL is not set — skipping.")
            return
        payload = {
            "item": item,
            "query": query,
            "found_at": datetime.now().isoformat(),
        }
        try:
            resp = requests.post(N8N_WEBHOOK_URL, json=payload, timeout=10)
            resp.raise_for_status()
            print(f"   📨 n8n notified for item {item.get('id')} ✓")
        except requests.RequestException as e:
            print(f"   ❌ n8n notification failed for item {item.get('id')}: {e}")

    def notify_new_items(self, items: list, query: str):
        """Dispatch new-item notifications to all enabled channels."""
        if not items:
            return

        if NOTIFY_N8N_ENABLED:
            for item in items:
                self._notify_n8n(item, query)

    # ── Simple search ─────────────────────────────────────────────────

    def run(self, query: str = DEFAULT_SEARCH_QUERY, catalog_id: int = None,
            price_from: float = None, price_to: float = None):
        """Run a single search and display results."""
        data = self.search(query, catalog_id=catalog_id,
                           price_from=price_from, price_to=price_to)
        if data:
            self.display_results(data)
        else:
            print("\n❌ Failed to fetch results. The API might require additional auth.")
            print("   Try again in a moment — Vinted may rate-limit requests.")


def main():
    parser = argparse.ArgumentParser(description="Search & monitor Vinted for items")
    parser.add_argument("query", nargs="?", default=DEFAULT_SEARCH_QUERY, help="Search query")
    parser.add_argument("--catalog", type=int, default=DEFAULT_CATALOG_ID,
                        help="Category ID (e.g. 2994 for Elektronika)")
    parser.add_argument("--price-from", type=float, default=DEFAULT_PRICE_FROM,
                        help="Minimum price filter")
    parser.add_argument("--price-to", type=float, default=DEFAULT_PRICE_TO,
                        help="Maximum price filter")
    parser.add_argument("--monitor", action="store_true",
                        help="Monitor mode: poll for new items periodically")
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL,
                        help=f"Poll interval in seconds (default: {POLL_INTERVAL})")
    args = parser.parse_args()

    scraper = VintedScraper()

    if args.monitor:
        scraper.monitor(args.query, catalog_id=args.catalog, interval=args.interval,
                        price_from=args.price_from, price_to=args.price_to)
    else:
        scraper.run(args.query, catalog_id=args.catalog,
                    price_from=args.price_from, price_to=args.price_to)


if __name__ == "__main__":
    main()
