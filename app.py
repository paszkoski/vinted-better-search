#!/usr/bin/env python3
"""Vinted Better Search — a Vinted search that actually respects your keywords."""

import os
import sys
import time
import threading
from datetime import datetime, timezone

from flask import Flask, render_template, request, jsonify

from scraper import VintedScraper
from matcher import parse_keywords, title_satisfies, full_satisfies
from categories import get_categories
from config import (
    DEFAULT_ORDER,
    DEFAULT_MAX_RESULTS,
    MAX_MAX_RESULTS,
    DEFAULT_MAX_SCAN,
    MAX_MAX_SCAN,
    CATALOG_PER_PAGE,
    VINTED_PROXY,
)

PORT = int(os.environ.get("PORT", 5000))
VALID_ORDERS = {"relevance", "newest_first", "price_high_to_low", "price_low_to_high"}

# ── Log-to-file (set LOG_FILE env var to enable) ──────────────────────────────

LOG_FILE = os.environ.get("LOG_FILE", "")

if LOG_FILE:
    class _Tee:
        def __init__(self, stream, path):
            self._stream = stream
            self._file = open(path, "a", encoding="utf-8", buffering=1)

        def write(self, data):
            self._stream.write(data)
            self._file.write(data)

        def flush(self):
            self._stream.flush()
            self._file.flush()

        def fileno(self):
            return self._stream.fileno()

    sys.stdout = _Tee(sys.stdout, LOG_FILE)
    sys.stderr = _Tee(sys.stderr, LOG_FILE)

app = Flask(__name__)

# A single shared scraper keeps session cookies warm across searches.
# Searches are serialized (one at a time) so concurrent requests can't
# interleave on the shared HTTP session / throttle state or double the
# request rate Vinted sees from this instance.
_scraper: VintedScraper | None = None
_scraper_lock = threading.Lock()
_search_lock = threading.Lock()


def get_scraper() -> VintedScraper:
    global _scraper
    with _scraper_lock:
        if _scraper is None:
            _scraper = VintedScraper()
        return _scraper


def build_result(item: dict, description: str | None) -> dict:
    price = item.get("price") or {}
    photo = item.get("photo") or {}
    return {
        "id": item.get("id"),
        "title": item.get("title", ""),
        "description": description or "",
        "price": price.get("amount"),
        "currency": price.get("currency_code", ""),
        "url": item.get("url", ""),
        "image_url": photo.get("url", ""),
        "brand": item.get("brand_title", ""),
        "size": item.get("size_title", ""),
        "status": item.get("status", ""),
        "favourite_count": item.get("favourite_count", 0),
        "listed_at": _photo_upload_date(photo),
        "seller_id": (item.get("user") or {}).get("id"),
        "country": None,  # filled in by run_search once results are final
    }


def _photo_upload_date(photo: dict) -> str | None:
    """Vinted doesn't expose a listing date directly — the main photo's
    upload timestamp is the closest available proxy for "date listed"."""
    timestamp = (photo.get("high_resolution") or {}).get("timestamp")
    if not timestamp:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()


def run_search(
    search_text: str,
    include_terms: list[str],
    exclude_terms: list[str],
    catalog_ids: list[int],
    price_from: float | None,
    price_to: float | None,
    order: str,
    max_results: int,
    max_scan: int,
) -> dict:
    """
    Scan Vinted search results and return only items where every include
    keyword appears in the title or description, and no exclude keyword
    does. Title-only matches skip the description fetch; everything else
    needs one extra request per candidate item.
    """
    scraper = get_scraper()
    started = time.time()

    results = []
    scanned = 0
    fetched = 0
    page = 1
    max_pages = 12
    search_session_id = None
    blocked = False

    while len(results) < max_results and scanned < max_scan and page <= max_pages:
        data = scraper.search(
            search_text,
            page=page,
            catalog_ids=catalog_ids or None,
            order=order,
            price_from=price_from,
            price_to=price_to,
            per_page=CATALOG_PER_PAGE,
            search_session_id=search_session_id,
        )
        search_session_id = data.get("search_session_id") or search_session_id
        items = data.get("items", [])
        if not items:
            if not data:
                blocked = True
            break

        # First pass: resolve what title alone can settle, and collect the
        # rest to fetch. Descriptions for a whole page are then fetched
        # concurrently (bounded by DETAIL_CONCURRENCY) instead of one at a
        # time — that's what keeps a search from taking minutes.
        needs_fetch = []  # (item, title) pairs
        for item in items:
            if scanned >= max_scan or len(results) >= max_results:
                break
            scanned += 1

            title = item.get("title", "")
            verdict = title_satisfies(title, include_terms, exclude_terms)
            if verdict is True:
                results.append(build_result(item, description=None))
            elif verdict is None:
                needs_fetch.append((item, title))

        if needs_fetch and len(results) < max_results:
            descriptions = scraper.get_item_descriptions(
                [(item.get("id"), item.get("url", "")) for item, _ in needs_fetch]
            )
            fetched += len(needs_fetch)
            for item, title in needs_fetch:
                description = descriptions.get(item.get("id"))
                if description is None:
                    # Couldn't verify (blocked / removed / network error) — skip
                    # rather than guess, so results never silently show a non-match.
                    continue
                if full_satisfies(title, description, include_terms, exclude_terms):
                    results.append(build_result(item, description=description))

        pagination = data.get("pagination", {})
        total_pages = pagination.get("total_pages", 1)
        if page >= total_pages:
            break
        page += 1

    # Country needs a per-seller profile fetch — only worth doing for the
    # items actually being shown, not every candidate that got scanned.
    seller_ids = [r["seller_id"] for r in results if r["seller_id"]]
    if seller_ids:
        countries = scraper.get_user_countries(seller_ids)
        for r in results:
            country = countries.get(r["seller_id"])
            if country:
                r["country"] = country.get("title") or country.get("code")
    for r in results:
        r.pop("seller_id", None)

    return {
        "results": results,
        "scanned": scanned,
        "fetched": fetched,
        "truncated": scanned >= max_scan and len(results) < max_results,
        "blocked": blocked,
        "elapsed_seconds": round(time.time() - started, 1),
    }


# ── Flask Routes ──────────────────────────────────────────────────────────────


@app.route("/")
def index():
    return render_template("index.html", proxy_configured=bool(VINTED_PROXY))


@app.route("/api/categories")
def api_categories():
    try:
        return jsonify({"ok": True, "categories": get_categories()})
    except Exception as e:
        print(f"[Categories] Fetch failed: {e}", flush=True)
        return jsonify({"ok": False, "error": "Couldn't fetch categories from Vinted."}), 502


@app.route("/api/search")
def api_search():
    include_terms = parse_keywords(request.args.get("q", ""))
    exclude_terms = parse_keywords(request.args.get("exclude", ""))

    if not include_terms:
        return jsonify({"ok": False, "error": "Enter at least one keyword."}), 400

    catalog_ids = []
    for raw in request.args.get("catalog_ids", "").split(","):
        raw = raw.strip()
        if raw.isdigit():
            catalog_ids.append(int(raw))

    def parse_float(name):
        raw = request.args.get(name, "").strip()
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    price_from = parse_float("price_from")
    price_to = parse_float("price_to")

    order = request.args.get("order", DEFAULT_ORDER)
    if order not in VALID_ORDERS:
        order = DEFAULT_ORDER

    def parse_int(name, default, cap):
        raw = request.args.get(name, "").strip()
        try:
            val = int(raw) if raw else default
        except ValueError:
            val = default
        return max(1, min(val, cap))

    max_results = parse_int("max_results", DEFAULT_MAX_RESULTS, MAX_MAX_RESULTS)
    max_scan = parse_int("max_scan", DEFAULT_MAX_SCAN, MAX_MAX_SCAN)
    max_scan = max(max_scan, max_results)

    # Send Vinted our include keywords as the search text too — it's a much
    # better candidate pool (roughly on-topic, sorted how we asked) than
    # scanning the whole catalog ourselves. We still verify every candidate
    # ourselves rather than trusting Vinted's match.
    search_text = " ".join(include_terms)

    if not _search_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "A search is already in progress. Try again in a moment."}), 429

    try:
        outcome = run_search(
            search_text=search_text,
            include_terms=include_terms,
            exclude_terms=exclude_terms,
            catalog_ids=catalog_ids,
            price_from=price_from,
            price_to=price_to,
            order=order,
            max_results=max_results,
            max_scan=max_scan,
        )
    finally:
        _search_lock.release()

    return jsonify({"ok": True, **outcome})


@app.route("/api/test-proxy", methods=["POST"])
def test_proxy():
    if not VINTED_PROXY:
        return jsonify({"ok": False, "error": "VINTED_PROXY env var is not set."})
    import requests
    from config import VINTED_BASE_URL
    proxy_url = f"http://{VINTED_PROXY}"
    proxies = {"http": proxy_url, "https": proxy_url}
    try:
        resp = requests.get(VINTED_BASE_URL, proxies=proxies, timeout=10,
                             headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code < 500:
            return jsonify({"ok": True, "status_code": resp.status_code})
        return jsonify({"ok": False, "error": f"Server returned {resp.status_code}."})
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


def _warm_categories():
    try:
        cats = get_categories()
        print(f"[Categories] Loaded {len(cats)} categories.", flush=True)
    except Exception as e:
        print(f"[Categories] Warm-up fetch failed (will retry on first request): {e}", flush=True)


if __name__ == "__main__":
    print(f"[App] Vinted Better Search starting on port {PORT}", flush=True)
    print(f"[App] Proxy: {'configured (' + VINTED_PROXY + ')' if VINTED_PROXY else 'not configured'}", flush=True)
    threading.Thread(target=_warm_categories, daemon=True).start()
    # threaded=True so /health and a long-running /api/search don't block each other;
    # _search_lock still serializes actual searches against the shared scraper.
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
