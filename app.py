#!/usr/bin/env python3
"""Vinted Monitor — Web interface for monitoring Vinted listings."""

import os
import time
import random
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from urllib.parse import urlparse, parse_qs

import requests
from flask import Flask, render_template, request, jsonify, redirect, url_for

from scraper import VintedScraper

# ── Configuration ─────────────────────────────────────────────────────────────

PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "")
N8N_WEBHOOK_URL = os.environ.get("N8N_WEBHOOK_URL", "")
API_KEY = os.environ.get("API_KEY", "")
VINTED_PROXY = os.environ.get("VINTED_PROXY", "")
PORT = int(os.environ.get("PORT", 5000))
DATA_DIR = os.environ.get(
    "DATA_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"),
)
DB_PATH = os.path.join(DATA_DIR, "vinted.db")
MIN_QUERY_DELAY = 10  # Minimum seconds between polling different queries

DEFAULT_PUSHOVER_TITLE = "New Vinted: {query}"
DEFAULT_PUSHOVER_MESSAGE = "{title} — {price}"

os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(__name__)

# ── Database ──────────────────────────────────────────────────────────────────


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS queries (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                name             TEXT,
                search_text      TEXT NOT NULL DEFAULT '',
                catalog_id       INTEGER,
                price_from       REAL,
                price_to         REAL,
                brand_ids        TEXT,
                size_ids         TEXT,
                color_ids        TEXT,
                material_ids     TEXT,
                status_ids       TEXT,
                interval_seconds INTEGER NOT NULL DEFAULT 300,
                enabled          INTEGER NOT NULL DEFAULT 1,
                last_checked     TEXT,
                last_seen_id     INTEGER,
                status           TEXT DEFAULT 'pending',
                created_at       TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS findings (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                query_id   INTEGER NOT NULL,
                item_id    INTEGER NOT NULL,
                title      TEXT,
                price      TEXT,
                url        TEXT,
                image_url  TEXT,
                found_at   TEXT NOT NULL,
                UNIQUE(query_id, item_id),
                FOREIGN KEY (query_id) REFERENCES queries(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        # Migrate existing databases: add new columns if not present
        for col in ('brand_ids', 'size_ids', 'color_ids', 'material_ids', 'status_ids'):
            try:
                conn.execute(f"ALTER TABLE queries ADD COLUMN {col} TEXT")
            except Exception:
                pass  # Column already exists


def get_setting(key: str, default: str = "") -> str:
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


def save_setting(key: str, value: str):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )


def apply_template(template: str, **vars) -> str:
    """Substitute {variable} placeholders. Unknown keys are left as-is."""
    try:
        return template.format(**vars)
    except (KeyError, ValueError):
        return template


def parse_ids(value) -> list:
    """Parse comma-separated IDs into a sorted, deduplicated list of ints."""
    if not value:
        return []
    return sorted({int(v.strip()) for v in str(value).split(',') if v.strip().isdigit()})


def ids_to_db(value: str):
    """Normalize comma-separated ID string for DB storage. Returns None if empty."""
    ids = parse_ids(value)
    return ','.join(str(i) for i in ids) if ids else None


# ── Pushover ──────────────────────────────────────────────────────────────────


def send_pushover(title: str, message: str, url: str = None) -> bool:
    """Send a Pushover notification. Logs result. Returns True on success."""
    if not (PUSHOVER_TOKEN and PUSHOVER_USER):
        print(
            "[Pushover] Skipped — credentials not set "
            "(PUSHOVER_TOKEN / PUSHOVER_USER env vars)",
            flush=True,
        )
        return False

    payload = {
        "token": PUSHOVER_TOKEN,
        "user": PUSHOVER_USER,
        "title": title[:250],
        "message": message[:1024],
    }
    if url:
        payload["url"] = url[:512]
        payload["url_title"] = "View on Vinted"

    try:
        resp = requests.post(
            "https://api.pushover.net/1/messages.json",
            data=payload,
            timeout=10,
        )
        data = resp.json()
        if data.get("status") == 1:
            print(f"[Pushover] Sent OK: '{title}'", flush=True)
            return True
        else:
            errors = data.get("errors", [resp.text])
            print(f"[Pushover] API error: {errors}", flush=True)
            return False
    except Exception as exc:
        print(f"[Pushover] Request failed: {exc}", flush=True)
        return False


# ── n8n Webhook ───────────────────────────────────────────────────────────────


def send_n8n_webhook(items: list, query_label: str, query_search_text: str, webhook_url: str) -> bool:
    """POST all new items for a query to an n8n webhook as a single JSON payload."""
    payload = {
        "items": items,        # full raw Vinted API items — all fields included
        "query": query_search_text,
        "query_label": query_label,
        "found_at": datetime.now().isoformat(),
    }
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        print(f"[n8n] Sent {len(items)} item(s) for '{query_search_text}'", flush=True)
        return True
    except Exception as exc:
        print(f"[n8n] Webhook failed for '{query_search_text}': {exc}", flush=True)
        return False


# ── Monitor Scheduler ─────────────────────────────────────────────────────────


class MonitorScheduler:
    """Background thread that polls Vinted queries on their configured schedules."""

    def __init__(self):
        self.running = False
        self._thread: threading.Thread = None
        self._scraper: VintedScraper = None
        self._force_check: set = set()
        self._lock = threading.Lock()

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False

    def schedule_now(self, query_id: int):
        """Request immediate check for a query on the next scheduler cycle."""
        with self._lock:
            self._force_check.add(query_id)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _notify_blocked(self, wait_minutes: int):
        """Send Pushover / n8n notifications when Vinted returns a 403 block."""
        resume_at = datetime.fromtimestamp(time.time() + wait_minutes * 60).strftime("%H:%M:%S")
        title = "Vinted Monitor blocked (403)"
        message = f"Vinted returned 403. All requests paused for {wait_minutes} min (resumes ~{resume_at})."

        if get_setting("notify_pushover_enabled", "1") == "1":
            send_pushover(title=f"🚫 {title}", message=message)

        if get_setting("notify_n8n_enabled", "0") == "1":
            webhook_url = get_setting("n8n_webhook_url", "") or N8N_WEBHOOK_URL
            if webhook_url:
                try:
                    resp = requests.post(webhook_url, json={
                        "event": "blocked",
                        "message": message,
                        "wait_minutes": wait_minutes,
                        "resume_at": resume_at,
                        "found_at": datetime.now().isoformat(),
                    }, timeout=10)
                    resp.raise_for_status()
                    print("[n8n] Block notification sent.", flush=True)
                except Exception as exc:
                    print(f"[n8n] Block notification failed: {exc}", flush=True)

    def _run(self):
        self._scraper = VintedScraper(on_blocked=self._notify_blocked)
        last_polled_time = 0.0

        while self.running:
            try:
                now = time.time()
                if now - last_polled_time >= MIN_QUERY_DELAY:
                    query = self._get_due_query(now)
                    if query:
                        self._poll_query(query)
                        last_polled_time = time.time()
            except Exception as exc:
                print(f"[Scheduler] Unhandled error: {exc}", flush=True)
            time.sleep(5)

    def _get_due_query(self, now: float) -> dict | None:
        """Return the next query due for polling, or None."""
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM queries WHERE enabled = 1"
            ).fetchall()

        with self._lock:
            force = self._force_check.copy()

        # Forced checks take priority
        for row in rows:
            if row["id"] in force:
                with self._lock:
                    self._force_check.discard(row["id"])
                return dict(row)

        # Find the most overdue enabled query
        candidates: list[tuple[float, dict]] = []
        for row in rows:
            lc = row["last_checked"]
            if lc is None:
                candidates.append((0.0, dict(row)))
            else:
                last_ts = datetime.fromisoformat(lc).timestamp()
                next_due = last_ts + row["interval_seconds"]
                if now >= next_due:
                    candidates.append((next_due, dict(row)))

        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]

    def _poll_query(self, query: dict):
        qid = query["id"]
        search_text = query["search_text"]
        catalog_id = query.get("catalog_id")
        price_from = query.get("price_from")
        price_to = query.get("price_to")
        brand_ids = parse_ids(query.get("brand_ids"))
        size_ids = parse_ids(query.get("size_ids"))
        color_ids = parse_ids(query.get("color_ids"))
        material_ids = parse_ids(query.get("material_ids"))
        status_ids = parse_ids(query.get("status_ids"))
        last_seen_id = query.get("last_seen_id")
        is_first_run = last_seen_id is None

        print(f"[Scheduler] Polling: '{search_text}'", flush=True)

        with get_db() as conn:
            conn.execute(
                "UPDATE queries SET status = 'checking' WHERE id = ?", (qid,)
            )

        try:
            new_items, newest_id = self._fetch_new_items(
                search_text, catalog_id, price_from, price_to,
                brand_ids, size_ids, color_ids, material_ids, status_ids,
                last_seen_id,
            )

            now_iso = datetime.now().isoformat()
            with get_db() as conn:
                conn.execute(
                    """UPDATE queries
                       SET status = 'ok', last_checked = ?,
                           last_seen_id = COALESCE(?, last_seen_id)
                       WHERE id = ?""",
                    (now_iso, newest_id, qid),
                )

                if not is_first_run and new_items:
                    for item in new_items:
                        price_info = item.get("price") or {}
                        price_str = (
                            f"{price_info.get('amount', '?')} "
                            f"{price_info.get('currency_code', '')}"
                        )
                        photo = item.get("photo") or {}
                        image_url = photo.get("url", "")
                        conn.execute(
                            """INSERT OR IGNORE INTO findings
                               (query_id, item_id, title, price, url, image_url, found_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (
                                qid,
                                item.get("id"),
                                item.get("title", ""),
                                price_str,
                                item.get("url", ""),
                                image_url,
                                now_iso,
                            ),
                        )

            if not is_first_run and new_items:
                label = query.get("name") or search_text
                print(
                    f"[Scheduler] {len(new_items)} new item(s) for '{search_text}'",
                    flush=True,
                )

                # ── Pushover ──────────────────────────────────────────────
                if get_setting("notify_pushover_enabled", "1") == "1":
                    title_tpl = get_setting("pushover_title", DEFAULT_PUSHOVER_TITLE)
                    msg_tpl = get_setting("pushover_message", DEFAULT_PUSHOVER_MESSAGE)

                    first = new_items[0]
                    first_price_info = first.get("price") or {}
                    first_price = (
                        f"{first_price_info.get('amount', '?')} "
                        f"{first_price_info.get('currency_code', '')}"
                    )
                    first_url = first.get("url", "")
                    first_vars = {
                        "query": label,
                        "title": first.get("title", "No title"),
                        "price": first_price,
                        "link": first_url,
                    }
                    notif_title = apply_template(title_tpl, **first_vars)
                    if len(new_items) > 1:
                        notif_title = f"({len(new_items)}) {notif_title}"

                    msg_lines = []
                    for item in new_items[:10]:
                        price_info = item.get("price") or {}
                        price_str = (
                            f"{price_info.get('amount', '?')} "
                            f"{price_info.get('currency_code', '')}"
                        )
                        v = {
                            "query": label,
                            "title": item.get("title", "No title"),
                            "price": price_str,
                            "link": item.get("url", ""),
                        }
                        msg_lines.append(apply_template(msg_tpl, **v))
                    if len(new_items) > 10:
                        msg_lines.append(f"… and {len(new_items) - 10} more")

                    send_pushover(
                        title=notif_title,
                        message="\n".join(msg_lines),
                        url=first_url,
                    )

                # ── n8n Webhook ───────────────────────────────────────────
                if get_setting("notify_n8n_enabled", "0") == "1":
                    webhook_url = get_setting("n8n_webhook_url", "") or N8N_WEBHOOK_URL
                    if webhook_url:
                        send_n8n_webhook(new_items, label, search_text, webhook_url)
                    else:
                        print("[n8n] Enabled but no webhook URL configured — skipping.", flush=True)
            else:
                suffix = " (baseline established)" if is_first_run else ""
                print(
                    f"[Scheduler] No new items for '{search_text}'{suffix}",
                    flush=True,
                )

        except Exception as exc:
            print(f"[Scheduler] Poll error for '{search_text}': {exc}", flush=True)
            with get_db() as conn:
                conn.execute(
                    "UPDATE queries SET status = 'error', last_checked = ? WHERE id = ?",
                    (datetime.now().isoformat(), qid),
                )

    def _fetch_new_items(
        self,
        search_text: str,
        catalog_id: int | None,
        price_from: float | None,
        price_to: float | None,
        brand_ids: list,
        size_ids: list,
        color_ids: list,
        material_ids: list,
        status_ids: list,
        last_seen_id: int | None,
    ) -> tuple[list, int | None]:
        """Fetch items newer than last_seen_id. Returns (new_items, newest_id)."""
        is_first_run = last_seen_id is None
        new_items = []
        newest_id = None
        page = 1

        while True:
            data = self._scraper.search(
                search_text,
                page=page,
                catalog_id=catalog_id,
                order="newest_first",
                price_from=price_from,
                price_to=price_to,
                brand_ids=brand_ids,
                size_ids=size_ids,
                color_ids=color_ids,
                material_ids=material_ids,
                status_ids=status_ids,
            )
            items = data.get("items", [])
            pagination = data.get("pagination", {})
            total_pages = pagination.get("total_pages", 1)

            if not items:
                break

            if newest_id is None:
                newest_id = items[0].get("id")

            if is_first_run:
                # First run: just establish the baseline (page 1 only)
                new_items.extend(items)
                break

            found_old = False
            for item in items:
                if item.get("id") <= last_seen_id:
                    found_old = True
                    break
                new_items.append(item)

            if found_old or page >= total_pages:
                break

            delay = random.uniform(1.0, 3.0)
            time.sleep(delay)
            page += 1

        return new_items, newest_id


scheduler = MonitorScheduler()

# ── API Auth ──────────────────────────────────────────────────────────────────


def require_api_key(f):
    """Decorator: reject requests without the correct API key.
    If API_KEY env var is not set, all requests are allowed (with a startup warning).
    Key is read from the X-API-Key header or api_key query param.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if API_KEY:
            provided = request.headers.get("X-API-Key") or request.args.get("api_key", "")
            if provided != API_KEY:
                return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


# ── Vinted URL Parser ─────────────────────────────────────────────────────────


def parse_vinted_url(url: str) -> dict:
    """Extract search parameters from a Vinted catalog URL.

    Returns a dict with keys: search_text, catalog_id, price_from, price_to,
    brand_ids, size_ids, color_ids, material_ids, status_ids.
    """
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    def first_float(key):
        try:
            return float(qs[key][0]) if key in qs else None
        except ValueError:
            return None

    def int_list(*keys):
        result = []
        for key in keys:
            for v in qs.get(key, []):
                try:
                    result.append(int(v))
                except ValueError:
                    pass
        return result

    search_text = qs.get("search_text", [""])[0].strip()
    # Vinted frontend uses catalog[] while the API uses catalog_ids[]
    catalog_id = int_list("catalog_ids[]", "catalog_ids", "catalog[]", "catalog")
    catalog_id = catalog_id[0] if catalog_id else None

    return {
        "search_text": search_text,
        "catalog_id": catalog_id,
        "price_from": first_float("price_from"),
        "price_to": first_float("price_to"),
        "brand_ids": int_list("brand_ids[]", "brand_ids"),
        "size_ids": int_list("size_ids[]", "size_ids"),
        "color_ids": int_list("color_ids[]", "color_ids"),
        "material_ids": int_list("material_ids[]", "material_ids"),
        "status_ids": int_list("status_ids[]", "status_ids"),
    }


# ── Flask Routes ──────────────────────────────────────────────────────────────


@app.route("/")
def index():
    with get_db() as conn:
        queries = conn.execute(
            """SELECT q.*, COUNT(f.id) AS findings_count
               FROM queries q
               LEFT JOIN findings f ON f.query_id = q.id
               GROUP BY q.id
               ORDER BY q.created_at DESC"""
        ).fetchall()
    n8n_webhook_url = get_setting("n8n_webhook_url", "") or N8N_WEBHOOK_URL
    settings = {
        "pushover_title": get_setting("pushover_title", DEFAULT_PUSHOVER_TITLE),
        "pushover_message": get_setting("pushover_message", DEFAULT_PUSHOVER_MESSAGE),
        "notify_pushover_enabled": get_setting("notify_pushover_enabled", "1") == "1",
        "notify_n8n_enabled": get_setting("notify_n8n_enabled", "0") == "1",
        "n8n_webhook_url": n8n_webhook_url,
    }
    return render_template(
        "index.html",
        queries=queries,
        pushover_configured=bool(PUSHOVER_TOKEN and PUSHOVER_USER),
        pushover_token_set=bool(PUSHOVER_TOKEN),
        pushover_user_set=bool(PUSHOVER_USER),
        n8n_webhook_set=bool(n8n_webhook_url),
        proxy_configured=bool(VINTED_PROXY),
        proxy_value=VINTED_PROXY,
        settings=settings,
    )


@app.route("/queries", methods=["POST"])
def add_query():
    name = request.form.get("name", "").strip() or None
    search_text = request.form.get("search_text", "").strip()

    catalog_id = request.form.get("catalog_id", "").strip()
    price_from = request.form.get("price_from", "").strip()
    price_to = request.form.get("price_to", "").strip()
    interval = request.form.get("interval_seconds", "300").strip()
    brand_ids_raw = request.form.get("brand_ids", "")
    size_ids_raw = request.form.get("size_ids", "")
    color_ids_raw = request.form.get("color_ids", "")
    material_ids_raw = request.form.get("material_ids", "")
    status_ids_raw = request.form.get("status_ids", "")

    # Require at least a search text or one filter
    has_filter = any([catalog_id, price_from, price_to,
                      brand_ids_raw, size_ids_raw, color_ids_raw,
                      material_ids_raw, status_ids_raw])
    if not search_text and not has_filter:
        return redirect(url_for("index"))

    with get_db() as conn:
        conn.execute(
            """INSERT INTO queries
               (name, search_text, catalog_id, price_from, price_to,
                brand_ids, size_ids, color_ids, material_ids, status_ids,
                interval_seconds, enabled, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'pending', ?)""",
            (
                name,
                search_text,
                int(catalog_id) if catalog_id else None,
                float(price_from) if price_from else None,
                float(price_to) if price_to else None,
                ids_to_db(brand_ids_raw),
                ids_to_db(size_ids_raw),
                ids_to_db(color_ids_raw),
                ids_to_db(material_ids_raw),
                ids_to_db(status_ids_raw),
                int(interval) if interval else 300,
                datetime.now().isoformat(),
            ),
        )
    return redirect(url_for("index"))


@app.route("/queries/<int:qid>/delete", methods=["POST"])
def delete_query(qid):
    with get_db() as conn:
        conn.execute("DELETE FROM queries WHERE id = ?", (qid,))
    return redirect(url_for("index"))


@app.route("/queries/<int:qid>/toggle", methods=["POST"])
def toggle_query(qid):
    with get_db() as conn:
        row = conn.execute(
            "SELECT enabled FROM queries WHERE id = ?", (qid,)
        ).fetchone()
        if not row:
            return redirect(url_for("index"))
        currently_enabled = row["enabled"]
        conn.execute(
            """UPDATE queries
               SET enabled = CASE WHEN enabled = 1 THEN 0 ELSE 1 END,
                   status  = CASE WHEN enabled = 1 THEN 'paused' ELSE 'pending' END
               WHERE id = ?""",
            (qid,),
        )
    # When resuming, schedule an immediate check so status moves to OK quickly
    if not currently_enabled:
        scheduler.schedule_now(qid)
    return redirect(url_for("index"))


@app.route("/queries/<int:qid>/check-now", methods=["POST"])
def check_now(qid):
    scheduler.schedule_now(qid)
    return redirect(url_for("index"))


@app.route("/queries/<int:qid>/edit", methods=["POST"])
def edit_query(qid):
    name = request.form.get("name", "").strip() or None
    search_text = request.form.get("search_text", "").strip()

    catalog_id = request.form.get("catalog_id", "").strip()
    price_from = request.form.get("price_from", "").strip()
    price_to = request.form.get("price_to", "").strip()
    interval = request.form.get("interval_seconds", "300").strip()

    new_catalog = int(catalog_id) if catalog_id else None
    new_price_from = float(price_from) if price_from else None
    new_price_to = float(price_to) if price_to else None
    new_interval = int(interval) if interval else 300
    new_brand_ids = ids_to_db(request.form.get("brand_ids", ""))
    new_size_ids = ids_to_db(request.form.get("size_ids", ""))
    new_color_ids = ids_to_db(request.form.get("color_ids", ""))
    new_material_ids = ids_to_db(request.form.get("material_ids", ""))
    new_status_ids = ids_to_db(request.form.get("status_ids", ""))

    with get_db() as conn:
        current = conn.execute(
            "SELECT * FROM queries WHERE id = ?", (qid,)
        ).fetchone()
        if not current:
            return redirect(url_for("index"))

        # Reset baseline only when the actual search parameters change
        search_changed = (
            current["search_text"] != search_text
            or current["catalog_id"] != new_catalog
            or current["price_from"] != new_price_from
            or current["price_to"] != new_price_to
            or (current["brand_ids"] or None) != new_brand_ids
            or (current["size_ids"] or None) != new_size_ids
            or (current["color_ids"] or None) != new_color_ids
            or (current["material_ids"] or None) != new_material_ids
            or (current["status_ids"] or None) != new_status_ids
        )

        conn.execute(
            """UPDATE queries
               SET name = ?, search_text = ?, catalog_id = ?,
                   price_from = ?, price_to = ?,
                   brand_ids = ?, size_ids = ?, color_ids = ?,
                   material_ids = ?, status_ids = ?,
                   interval_seconds = ?
               WHERE id = ?""",
            (name, search_text, new_catalog, new_price_from, new_price_to,
             new_brand_ids, new_size_ids, new_color_ids, new_material_ids,
             new_status_ids, new_interval, qid),
        )
        if search_changed:
            conn.execute(
                """UPDATE queries
                   SET last_seen_id = NULL, last_checked = NULL, status = 'pending'
                   WHERE id = ?""",
                (qid,),
            )

    if search_changed:
        scheduler.schedule_now(qid)

    return redirect(url_for("index"))


@app.route("/settings", methods=["POST"])
def save_settings():
    title = request.form.get("pushover_title", "").strip()
    message = request.form.get("pushover_message", "").strip()
    save_setting("pushover_title", title or DEFAULT_PUSHOVER_TITLE)
    save_setting("pushover_message", message or DEFAULT_PUSHOVER_MESSAGE)

    # Toggles — checkboxes are absent from the POST body when unchecked
    save_setting("notify_pushover_enabled", "1" if request.form.get("notify_pushover_enabled") else "0")
    save_setting("notify_n8n_enabled", "1" if request.form.get("notify_n8n_enabled") else "0")

    n8n_url = request.form.get("n8n_webhook_url", "").strip()
    save_setting("n8n_webhook_url", n8n_url)

    return redirect(url_for("index"))


# ── JSON API ──────────────────────────────────────────────────────────────────


@app.route("/api/queries")
def api_queries():
    with get_db() as conn:
        rows = conn.execute(
            """SELECT q.*, COUNT(f.id) AS findings_count
               FROM queries q
               LEFT JOIN findings f ON f.query_id = q.id
               GROUP BY q.id
               ORDER BY q.created_at DESC"""
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/findings")
def api_findings():
    query_id = request.args.get("query_id", type=int)
    limit = min(request.args.get("limit", 60, type=int), 200)
    sql = """SELECT f.*, COALESCE(q.name, q.search_text) AS query_label
             FROM findings f
             JOIN queries q ON f.query_id = q.id"""
    params = []
    if query_id:
        sql += " WHERE f.query_id = ?"
        params.append(query_id)
    sql += " ORDER BY f.found_at DESC LIMIT ?"
    params.append(limit)
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/test-pushover", methods=["POST"])
def test_pushover():
    if not (PUSHOVER_TOKEN and PUSHOVER_USER):
        return jsonify({
            "ok": False,
            "error": "PUSHOVER_TOKEN and PUSHOVER_USER env vars are not set.",
        })
    title_tpl = get_setting("pushover_title", DEFAULT_PUSHOVER_TITLE)
    msg_tpl = get_setting("pushover_message", DEFAULT_PUSHOVER_MESSAGE)
    tpl_vars = {
        "query": "Test Query",
        "title": "Sample Item — Like New",
        "price": "149 PLN",
        "link": "https://www.vinted.pl",
    }
    ok = send_pushover(
        title=apply_template(title_tpl, **tpl_vars),
        message=apply_template(msg_tpl, **tpl_vars),
        url="https://www.vinted.pl",
    )
    return jsonify({"ok": ok, "error": None if ok else "Check container logs for details."})


@app.route("/api/test-n8n", methods=["POST"])
def test_n8n():
    # Accept URL from JSON body (live test before saving) or fall back to saved/env value
    body = request.get_json(silent=True) or {}
    webhook_url = body.get("webhook_url", "").strip() or get_setting("n8n_webhook_url", "") or N8N_WEBHOOK_URL
    if not webhook_url:
        return jsonify({"ok": False, "error": "No n8n webhook URL configured."})
    test_item = {
        "id": 0,
        "title": "Test Item — Vinted Monitor",
        "url": "https://www.vinted.pl",
        "description": "This is a test notification from Vinted Monitor.",
        "brand_title": "Test Brand",
        "status": "New with tags",
        "size_title": "M",
        "price": {"amount": "149.00", "currency_code": "PLN"},
        "total_item_price": {"amount": "165.00", "currency_code": "PLN"},
        "photo": {"url": None},
        "favourite_count": 0,
        "user": {"id": 0, "login": "test_seller"},
    }
    ok = send_n8n_webhook([test_item], "Test Query", "test query", webhook_url)
    return jsonify({"ok": ok, "error": None if ok else "Check container logs for details."})


@app.route("/api/test-proxy", methods=["POST"])
def test_proxy():
    if not VINTED_PROXY:
        return jsonify({"ok": False, "error": "VINTED_PROXY env var is not set."})
    proxy_url = f"http://{VINTED_PROXY}"
    proxies = {"http": proxy_url, "https": proxy_url}
    from config import VINTED_BASE_URL
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


# ── Control API (v1) ──────────────────────────────────────────────────────────
# All endpoints require X-API-Key header (or ?api_key=) when API_KEY is set.
# Designed for use with n8n / WhatsApp automation.


@app.route("/api/v1/queries", methods=["GET"])
@require_api_key
def v1_list_queries():
    """List all queries with their current status and findings count."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT q.*, COUNT(f.id) AS findings_count
               FROM queries q
               LEFT JOIN findings f ON f.query_id = q.id
               GROUP BY q.id
               ORDER BY q.created_at DESC"""
        ).fetchall()
    return jsonify({"ok": True, "queries": [dict(r) for r in rows]})


@app.route("/api/v1/queries", methods=["POST"])
@require_api_key
def v1_add_query():
    """Add a new query.

    Accepts JSON with either:
      - url: a Vinted catalog URL (parameters extracted automatically)
      - search_text and optional filters

    Optional fields (all): name, interval_seconds, catalog_id, price_from,
    price_to, brand_ids, size_ids, color_ids, material_ids, status_ids.
    """
    # Accept params from JSON body, form data, or query string (n8n sends via query string)
    body = request.get_json(silent=True, force=True) or request.form.to_dict() or request.args.to_dict() or {}

    if "url" in body:
        params = parse_vinted_url(body["url"])
        # Allow explicit overrides on top of URL-parsed values
        search_text = body.get("search_text", params["search_text"]).strip()
        catalog_id = body.get("catalog_id", params["catalog_id"])
        price_from = body.get("price_from", params["price_from"])
        price_to = body.get("price_to", params["price_to"])
        brand_ids = body.get("brand_ids", params["brand_ids"])
        size_ids = body.get("size_ids", params["size_ids"])
        color_ids = body.get("color_ids", params["color_ids"])
        material_ids = body.get("material_ids", params["material_ids"])
        status_ids = body.get("status_ids", params["status_ids"])
    else:
        search_text = body.get("search_text", "").strip()
        catalog_id = body.get("catalog_id")
        price_from = body.get("price_from")
        price_to = body.get("price_to")
        brand_ids = body.get("brand_ids", [])
        size_ids = body.get("size_ids", [])
        color_ids = body.get("color_ids", [])
        material_ids = body.get("material_ids", [])
        status_ids = body.get("status_ids", [])

    has_filter = any([catalog_id, price_from, price_to,
                      brand_ids, size_ids, color_ids, material_ids, status_ids])
    if not search_text and not has_filter:
        return jsonify({"ok": False, "error": "Provide a Vinted url or at least search_text / one filter"}), 400

    name = body.get("name", "").strip() or None
    interval = int(body.get("interval_seconds", 300))

    def ids_str(val):
        """Accept list of ints or comma string, normalise to DB format."""
        if isinstance(val, list):
            return ids_to_db(",".join(str(v) for v in val))
        return ids_to_db(str(val) if val else "")

    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO queries
               (name, search_text, catalog_id, price_from, price_to,
                brand_ids, size_ids, color_ids, material_ids, status_ids,
                interval_seconds, enabled, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'pending', ?)""",
            (
                name,
                search_text,
                int(catalog_id) if catalog_id is not None else None,
                float(price_from) if price_from is not None else None,
                float(price_to) if price_to is not None else None,
                ids_str(brand_ids),
                ids_str(size_ids),
                ids_str(color_ids),
                ids_str(material_ids),
                ids_str(status_ids),
                interval,
                datetime.now().isoformat(),
            ),
        )
        new_id = cur.lastrowid

    scheduler.schedule_now(new_id)
    return jsonify({"ok": True, "id": new_id}), 201


@app.route("/api/v1/queries/<int:qid>", methods=["DELETE"])
@require_api_key
def v1_delete_query(qid):
    """Delete a query and all its findings."""
    with get_db() as conn:
        row = conn.execute("SELECT id FROM queries WHERE id = ?", (qid,)).fetchone()
        if not row:
            return jsonify({"ok": False, "error": "Query not found"}), 404
        conn.execute("DELETE FROM queries WHERE id = ?", (qid,))
    return jsonify({"ok": True})


@app.route("/api/v1/queries/<int:qid>/toggle", methods=["POST"])
@require_api_key
def v1_toggle_query(qid):
    """Enable or disable a query. Returns the new enabled state."""
    with get_db() as conn:
        row = conn.execute("SELECT enabled FROM queries WHERE id = ?", (qid,)).fetchone()
        if not row:
            return jsonify({"ok": False, "error": "Query not found"}), 404
        currently_enabled = row["enabled"]
        conn.execute(
            """UPDATE queries
               SET enabled = CASE WHEN enabled = 1 THEN 0 ELSE 1 END,
                   status  = CASE WHEN enabled = 1 THEN 'paused' ELSE 'pending' END
               WHERE id = ?""",
            (qid,),
        )
    if currently_enabled:
        return jsonify({"ok": True, "enabled": False})
    scheduler.schedule_now(qid)
    return jsonify({"ok": True, "enabled": True})


@app.route("/api/v1/queries/<int:qid>/check-now", methods=["POST"])
@require_api_key
def v1_check_now(qid):
    """Trigger an immediate check for a query."""
    with get_db() as conn:
        row = conn.execute("SELECT id FROM queries WHERE id = ?", (qid,)).fetchone()
        if not row:
            return jsonify({"ok": False, "error": "Query not found"}), 404
    scheduler.schedule_now(qid)
    return jsonify({"ok": True})


@app.route("/api/v1/findings", methods=["GET"])
@require_api_key
def v1_findings():
    """Recent findings across all queries. Optional ?query_id=N and ?limit=N (default 60)."""
    query_id = request.args.get("query_id", type=int)
    limit = min(request.args.get("limit", 60, type=int), 200)

    sql = """SELECT f.*, COALESCE(q.name, q.search_text) AS query_label
             FROM findings f
             JOIN queries q ON f.query_id = q.id"""
    params = []
    if query_id:
        sql += " WHERE f.query_id = ?"
        params.append(query_id)
    sql += " ORDER BY f.found_at DESC LIMIT ?"
    params.append(limit)

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return jsonify({"ok": True, "findings": [dict(r) for r in rows]})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    scheduler.start()
    print(f"[App] Vinted Monitor starting on port {PORT}", flush=True)
    print(
        f"[App] Pushover: {'configured' if PUSHOVER_TOKEN and PUSHOVER_USER else 'not configured'}",
        flush=True,
    )
    print(
        f"[App] n8n webhook: {'configured' if N8N_WEBHOOK_URL else 'not configured (set via UI or N8N_WEBHOOK_URL env var)'}",
        flush=True,
    )
    if API_KEY:
        print("[App] Control API: enabled (API_KEY is set)", flush=True)
    else:
        print("[App] Control API: WARNING — API_KEY not set, /api/v1/* endpoints are unprotected", flush=True)
    app.run(host="0.0.0.0", port=PORT, debug=False)
