#!/usr/bin/env python3
"""Vinted Better Search — a Vinted search that actually respects your keywords."""

import os
import subprocess
import sys
import time
import threading
from datetime import datetime, timezone

from flask import Flask, render_template, request, jsonify

from scraper import VintedScraper
from matcher import (
    parse_keyword_groups,
    parse_keywords,
    listing_satisfies,
    full_satisfies,
    first_exclude_hit,
    missing_groups,
)
from categories import get_categories
from config import (
    DEFAULT_ORDER,
    CATALOG_PER_PAGE,
    MAX_SEARCH_PAGES,
    VINTED_BASE_URL,
    DEEP_SEARCH_MAX_ITEMS,
)

PORT = int(os.environ.get("PORT", 5000))
VALID_ORDERS = {"relevance", "newest_first", "price_high_to_low", "price_low_to_high"}
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
_update_lock = threading.Lock()

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

# Live progress for whatever search is currently running, polled by the
# frontend from /api/search/progress. _search_lock means at most one search
# is ever in flight, so a single global dict is enough — no per-request key.
# request/retry/failed counts are recomputed live from the scraper on every
# poll (against this baseline) rather than only at each _report() call —
# those calls span a blocking concurrent fetch of dozens of items, so a
# stored count would sit stale for the whole batch instead of ticking as
# individual requests complete.
_progress_lock = threading.Lock()
_progress: dict = {"active": False}
_progress_baseline = {"requests": 0, "retries": 0, "failed": 0}


def get_scraper() -> VintedScraper:
    global _scraper
    with _scraper_lock:
        if _scraper is None:
            _scraper = VintedScraper()
        return _scraper


def _set_progress(**kwargs):
    with _progress_lock:
        _progress.update(kwargs)


def _item_url(item: dict) -> str:
    """Vinted's search API now returns a host-relative item path
    (e.g. "/items/123-foo") instead of a full URL."""
    url = item.get("url", "")
    if url and url.startswith("/"):
        url = VINTED_BASE_URL + url
    return url


def build_result(item: dict, description: str | None) -> dict:
    price = item.get("price") or {}
    photo = item.get("photo") or {}
    return {
        "id": item.get("id"),
        "title": item.get("title", ""),
        "description": description or "",
        "price": price.get("amount"),
        "currency": price.get("currency_code", ""),
        "url": _item_url(item),
        "image_url": photo.get("url", ""),
        "brand": item.get("brand_title", ""),
        "size": item.get("size_title", ""),
        "status": item.get("status", ""),
        "favourite_count": item.get("favourite_count", 0),
        "listed_at": _photo_upload_date(photo),
        "seller_id": (item.get("user") or {}).get("id"),
        # Left empty by quick search — filling it needs a per-seller profile
        # request, so only Deep Search pays for it.
        "country": None,
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
    include_groups: list[list[str]],
    exclude_terms: list[str],
    catalog_ids: list[int],
    price_from: float | None,
    price_to: float | None,
    order: str,
    show_partial: bool,
) -> dict:
    """
    Stage 1 — scan Vinted search results and match on listing fields only
    (title + brand), which every candidate already carries. Deliberately
    fetches nothing per item: no item pages, no seller profiles. Items whose
    listing doesn't settle the match are simply not results; the description
    hunt is what Deep Search (below) does, and only for selected items.

    Pages through everything Vinted reports (stopping on an empty page or
    once `pagination.total_pages` is reached), with a `MAX_SEARCH_PAGES`
    safety ceiling so a very broad query can't fire dozens of back-to-back
    catalog requests.

    With `show_partial`, candidates matching only *some* include groups are
    also returned (flagged `partial` + the groups they're `missing`), so the
    user can Deep Search them to check the description — but only when at
    least one group did match, so Vinted's fuzzy pool doesn't flood the grid.
    """
    scraper = get_scraper()
    started = time.time()
    requests_before = scraper.request_count
    retries_before = scraper.retry_count
    failed_before = scraper.failed_count
    _progress_baseline.update(requests=requests_before, retries=retries_before, failed=failed_before)

    def _report(**extra):
        _set_progress(
            requests_sent=scraper.request_count - requests_before,
            retries=scraper.retry_count - retries_before,
            failed=scraper.failed_count - failed_before,
            **extra,
        )

    results = []
    partials = []
    scanned = 0
    page = 1
    pages = 0          # pages actually fetched
    total_pages = 0    # Vinted's reported page count, known after page 1
    search_session_id = None
    blocked = False
    hit_page_limit = False

    _set_progress(active=True, phase="searching", page=0, pages=0, total_pages=0,
                   scanned=0, found=0, to_fetch=0, requests_sent=0, retries=0, failed=0)

    try:
        # The `else` runs only if the ceiling stopped us — i.e. Vinted still
        # had pages left but we deliberately didn't fetch them.
        while page <= MAX_SEARCH_PAGES:
            _report(phase="searching", page=page, total_pages=total_pages)
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
            pages += 1
            search_session_id = (
                (data.get("search_tracking_params") or {}).get("search_session_id")
                or search_session_id
            )
            items = data.get("items", [])
            if not items:
                if not data:
                    blocked = True
                break

            pagination = data.get("pagination", {})
            total_pages = pagination.get("total_pages", 1) or total_pages

            for item in items:
                scanned += 1

                # Listing fields only — "brand_title" rides along in the same
                # search response as "title", so this costs zero requests.
                listing_text = f"{item.get('title', '')} {item.get('brand_title', '')}"
                if listing_satisfies(listing_text, include_groups, exclude_terms):
                    result = build_result(item, description=None)
                    result["partial"] = False
                    results.append(result)
                    continue

                # Not a full match. An exclude term rejects outright; otherwise
                # include groups are missing — a partial only when it was asked
                # for and at least one group did match (a candidate matching no
                # group at all is just Vinted's fuzzy pool leaking through).
                if first_exclude_hit(listing_text, exclude_terms):
                    continue
                missing = missing_groups(listing_text, include_groups)
                if show_partial and len(missing) < len(include_groups):
                    partial = build_result(item, description=None)
                    partial["partial"] = True
                    partial["missing"] = [" OR ".join(group) for group in missing]
                    partials.append(partial)

            _report(phase="searching", page=page, total_pages=total_pages,
                    scanned=scanned, found=len(results))

            if page >= total_pages:
                break
            page += 1
        else:
            hit_page_limit = True
    finally:
        _report(active=False, phase="done", to_fetch=0)

    return {
        # Full matches first, then partials — the frontend renders them in that
        # order with a divider between.
        "results": results + partials,
        "partial_count": len(partials),
        "scanned": scanned,
        "pages": pages,
        "hit_page_limit": hit_page_limit,
        "blocked": blocked,
        "elapsed_seconds": round(time.time() - started, 1),
        "requests_sent": scraper.request_count - requests_before,
        "failed_requests": scraper.failed_count - failed_before,
    }


def run_deep_search(
    items: list[dict],
    include_groups: list[list[str]],
    exclude_terms: list[str],
) -> dict:
    """
    Stage 2 — for the items the user explicitly selected, fetch the item page
    (for the description) and the seller profile (for country), then re-check
    each against title + description.

    Every selected item gets a verdict, and cards are never silently dropped:
    `match` (verified), `excluded` (an exclude term only in the description),
    `unverified` (page couldn't be fetched — retryable), or `no_match`
    (fetched, but still missing an include group — only reachable if the item
    was deep-searched with different keywords than produced it).
    """
    scraper = get_scraper()
    started = time.time()
    requests_before = scraper.request_count
    retries_before = scraper.retry_count
    failed_before = scraper.failed_count
    _progress_baseline.update(requests=requests_before, retries=retries_before, failed=failed_before)

    def _report(**extra):
        _set_progress(
            requests_sent=scraper.request_count - requests_before,
            retries=scraper.retry_count - retries_before,
            failed=scraper.failed_count - failed_before,
            **extra,
        )

    outcomes = []
    _set_progress(active=True, phase="deep: checking descriptions", to_fetch=len(items),
                   checked=0, requests_sent=0, retries=0, failed=0)

    try:
        descriptions = scraper.get_item_descriptions(
            [(item["id"], item["url"]) for item in items]
        )
        checked = 0
        for item in items:
            description = descriptions.get(item["id"])
            row = {
                "id": item["id"],
                "description": "",
                "excluded_by": None,
                "country": None,
            }
            if description is None:
                # Blocked / removed / network error. Say so honestly instead of
                # guessing either way, and leave the item checked so the user
                # can just hit Deep Search again.
                row["verdict"] = "unverified"
            else:
                row["description"] = description
                title = item.get("title", "")
                excluded_by = first_exclude_hit(f"{title} {description}", exclude_terms)
                if excluded_by:
                    row["verdict"] = "excluded"
                    row["excluded_by"] = excluded_by
                elif full_satisfies(title, description, include_groups, exclude_terms):
                    row["verdict"] = "match"
                else:
                    row["verdict"] = "no_match"
            outcomes.append(row)
            checked += 1
            _report(phase="deep: checking descriptions", checked=checked,
                    to_fetch=len(items) - checked)

        # Country is a per-seller profile fetch; do it for every selected item
        # (useful even on excluded cards) but each seller only once — the
        # scraper caches by user id.
        seller_ids = [item.get("seller_id") for item in items if item.get("seller_id")]
        if seller_ids:
            _report(phase="deep: resolving seller countries", to_fetch=len(seller_ids))
            countries = scraper.get_user_countries(seller_ids)
            rows_by_id = {row["id"]: row for row in outcomes}
            for item in items:
                country = countries.get(item.get("seller_id"))
                if country:
                    rows_by_id[item["id"]]["country"] = country.get("title") or country.get("code")
    finally:
        _report(active=False, phase="done", to_fetch=0)

    return {
        "items": outcomes,
        "elapsed_seconds": round(time.time() - started, 1),
        "requests_sent": scraper.request_count - requests_before,
        "failed_requests": scraper.failed_count - failed_before,
    }


# ── Flask Routes ──────────────────────────────────────────────────────────────


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/categories")
def api_categories():
    try:
        return jsonify({"ok": True, "categories": get_categories()})
    except Exception as e:
        print(f"[Categories] Fetch failed: {e}", flush=True)
        return jsonify({"ok": False, "error": "Couldn't fetch categories from Vinted."}), 502


@app.route("/api/search")
def api_search():
    include_groups = parse_keyword_groups(request.args.get("q", ""))
    exclude_terms = parse_keywords(request.args.get("exclude", ""))

    if not include_groups:
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

    # Send Vinted our include keywords as the search text too — it's a much
    # better candidate pool (roughly on-topic, sorted how we asked) than
    # scanning the whole catalog ourselves. We still verify every candidate
    # ourselves rather than trusting Vinted's match. One alternative per
    # OR-group is enough here — this only shapes the candidate pool, the
    # actual OR logic is enforced by our own matching afterwards.
    search_text = " ".join(group[0] for group in include_groups)

    # Opt-in: also return candidates matching only some include groups.
    show_partial = request.args.get("partial", "").strip().lower() in ("1", "true", "on", "yes")

    if not _search_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "A search is already in progress. Try again in a moment."}), 429

    try:
        outcome = run_search(
            search_text=search_text,
            include_groups=include_groups,
            exclude_terms=exclude_terms,
            catalog_ids=catalog_ids,
            price_from=price_from,
            price_to=price_to,
            order=order,
            show_partial=show_partial,
        )
    finally:
        _search_lock.release()

    # The frontend needs the Deep Search cap up front so it can stop the user
    # from selecting more than the server will accept.
    return jsonify({"ok": True, "deep_search_max_items": DEEP_SEARCH_MAX_ITEMS, **outcome})


@app.route("/api/deep-search", methods=["POST"])
def api_deep_search():
    # Stage 2 of the two-stage flow (see run_deep_search). The client sends
    # back the search's keywords plus the specific items it wants checked, so
    # this never re-runs the whole search — just the selected item pages.
    payload = request.get_json(silent=True) or {}

    include_groups = parse_keyword_groups(payload.get("q", ""))
    exclude_terms = parse_keywords(payload.get("exclude", ""))
    if not include_groups:
        return jsonify({"ok": False, "error": "Enter at least one keyword."}), 400

    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        return jsonify({"ok": False, "error": "Select at least one item to deep-search."}), 400
    if len(raw_items) > DEEP_SEARCH_MAX_ITEMS:
        return jsonify({
            "ok": False,
            "error": f"Too many items — Deep Search handles up to {DEEP_SEARCH_MAX_ITEMS} at a time.",
        }), 400

    # The client hands us URLs to fetch, so treat the whole payload as
    # untrusted: ids must be real ints and every URL must live on the
    # configured Vinted host. Requiring the "/" boundary keeps a look-alike
    # host (e.g. https://www.vinted.pl.evil.example) from sneaking through
    # startswith() and turning this into an SSRF proxy.
    vinted_prefix = VINTED_BASE_URL.rstrip("/") + "/"
    items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            return jsonify({"ok": False, "error": "Malformed item in Deep Search request."}), 400
        item_id = raw.get("id")
        url = raw.get("url")
        if not isinstance(item_id, int) or isinstance(item_id, bool):
            return jsonify({"ok": False, "error": "Malformed item id in Deep Search request."}), 400
        if not isinstance(url, str) or not url.startswith(vinted_prefix):
            return jsonify({"ok": False, "error": "Item URL is not on the configured Vinted domain."}), 400
        seller_id = raw.get("seller_id")
        if not isinstance(seller_id, int) or isinstance(seller_id, bool):
            seller_id = None
        items.append({
            "id": item_id,
            "url": url,
            "title": raw.get("title") if isinstance(raw.get("title"), str) else "",
            "brand": raw.get("brand") if isinstance(raw.get("brand"), str) else "",
            "seller_id": seller_id,
        })

    if not _search_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "A search is already in progress. Try again in a moment."}), 429

    try:
        outcome = run_deep_search(
            items=items,
            include_groups=include_groups,
            exclude_terms=exclude_terms,
        )
    finally:
        _search_lock.release()

    return jsonify({"ok": True, **outcome})


@app.route("/api/search/progress")
def api_search_progress():
    with _progress_lock:
        snapshot = dict(_progress)
    if snapshot.get("active"):
        scraper = get_scraper()
        snapshot["requests_sent"] = scraper.request_count - _progress_baseline["requests"]
        snapshot["retries"] = scraper.retry_count - _progress_baseline["retries"]
        snapshot["failed"] = scraper.failed_count - _progress_baseline["failed"]
    return jsonify(snapshot)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()


def _git(*args, timeout=15):
    return subprocess.run(
        ["git", *args],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _redact(text: str) -> str:
    return text.replace(GITHUB_TOKEN, "***") if GITHUB_TOKEN else text


def _remote_url() -> str:
    """Origin URL, with the PAT injected for this call only — never written
    to .git/config, since the repo is private and origin is plain https."""
    url = _git("config", "--get", "remote.origin.url").stdout.strip()
    if GITHUB_TOKEN and url.startswith("https://"):
        return url.replace("https://", f"https://{GITHUB_TOKEN}@", 1)
    return url


@app.route("/api/version")
def api_version():
    if not GITHUB_TOKEN:
        return jsonify({"ok": False, "error": "GITHUB_TOKEN not set — required for a private repo."}), 500
    try:
        branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        local = _git("rev-parse", "HEAD").stdout.strip()
        remote_proc = _git("ls-remote", _remote_url(), branch, timeout=10)
        remote_line = remote_proc.stdout.strip()
        remote = remote_line.split()[0] if remote_line else None
        if not remote:
            return jsonify({"ok": False, "error": _redact(remote_proc.stderr.strip()) or "Couldn't reach GitHub."}), 502
        return jsonify({
            "ok": True,
            "branch": branch,
            "local": local[:7],
            "remote": remote[:7],
            "update_available": remote != local,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": _redact(str(e))}), 502


@app.route("/api/update", methods=["POST"])
def api_update():
    if not GITHUB_TOKEN:
        return jsonify({"ok": False, "error": "GITHUB_TOKEN not set — required for a private repo."}), 500
    if not _update_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "An update is already in progress."}), 429

    try:
        branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        pull = _git("pull", "--ff-only", _remote_url(), branch, timeout=60)
        if pull.returncode != 0:
            _update_lock.release()
            return jsonify({"ok": False, "error": _redact(pull.stderr.strip()) or "git pull failed."}), 500
    except Exception as e:
        _update_lock.release()
        return jsonify({"ok": False, "error": _redact(str(e))}), 500

    def _restart():
        # Give the response time to reach the browser, then exit — the
        # container's restart policy brings the process back up on the
        # freshly-pulled code. The lock is intentionally never released:
        # the process is about to die.
        time.sleep(1)
        os._exit(0)

    threading.Thread(target=_restart, daemon=True).start()
    return jsonify({"ok": True, "message": "Updated. Restarting…"})


def _warm_categories():
    try:
        cats = get_categories()
        print(f"[Categories] Loaded {len(cats)} categories.", flush=True)
    except Exception as e:
        print(f"[Categories] Warm-up fetch failed (will retry on first request): {e}", flush=True)


if __name__ == "__main__":
    print(f"[App] Vinted Better Search starting on port {PORT}", flush=True)
    print("[App] IP-block defense: NordVPN rotation (see vpn.py)", flush=True)
    threading.Thread(target=_warm_categories, daemon=True).start()
    # threaded=True so /health and a long-running /api/search don't block each other;
    # _search_lock still serializes actual searches against the shared scraper.
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
