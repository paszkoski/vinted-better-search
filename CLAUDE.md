# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Vinted Better Search — a Flask app that wraps Vinted's search API with strict, literal
keyword matching against title *and* description. Vinted's own search is fuzzy and
frequently ignores the words a user types (and its `svc-catalogue/items` API doesn't return
descriptions at all), so this app re-verifies every candidate itself.

## Running

No test suite, linter, or build step exists in this repo.

```bash
pip install -r requirements.txt
python app.py                    # serves on :5000 (PORT env var to override)
```

Or via Docker (builds an image with the NordVPN Linux client baked in — see
[Dockerfile](Dockerfile) and [docker-entrypoint.sh](docker-entrypoint.sh) — then runs `app.py`;
needs `NORDVPN_TOKEN` set, and `NET_ADMIN`/`/dev/net/tun` are already wired up in
[docker-compose.yml](docker-compose.yml)):

```bash
docker compose up
```

Manual scraper testing without the web app:

```bash
python scraper.py "xreal beam pro"
python scraper.py "xreal beam pro" --catalog 2994 --price-from 100 --price-to 500
```

Config is entirely environment variables, read in [config.py](config.py) — see
[.env.example](.env.example) for the full list (Vinted country domain, request throttling,
NordVPN token/countries, block cooldown fallback, `GITHUB_TOKEN` for the in-app self-update
button).

## Architecture

Five modules, each with one job:

- **[scraper.py](scraper.py)** — `VintedScraper`: talks to Vinted. Spoofs a real iOS app
  session (rotating device/session identifiers, mobile `User-Agent` profiles) since Vinted
  blocks obvious bot traffic with a 403. Handles session refresh, request throttling, and
  per-instance in-memory caches for item descriptions and seller countries. On a 403, calls
  `vpn.rotate()` to move to a new NordVPN server and retries the request once before falling
  back to a `BLOCK_WAIT_SECONDS` cooldown (only reached if the rotation itself fails).
  `search()` hits `svc-catalogue/items` on the `api.` host (titles only, no description;
  item URLs it returns are host-relative, so callers must prefix `VINTED_BASE_URL`).
  `get_item_description()` fetches an item's public page and pulls the description out of
  the JSON-LD `Product` block Vinted embeds there — this is the only way to get description
  text. Both description and country fetches have `_concurrent` batch variants bounded by
  `DETAIL_CONCURRENCY` via a semaphore, since fetching one at a time is what makes a search
  slow.

- **[vpn.py](vpn.py)** — thin wrapper around `nordvpn_switcher`, which drives the NordVPN
  Linux CLI already logged in inside the container (see Dockerfile/docker-entrypoint.sh).
  `rotate()` is process-wide (VPN rotation changes the whole container's egress IP, not a
  per-request thing) — serialized behind a lock with a `VPN_ROTATE_COOLDOWN_SECONDS` cooldown
  so concurrent detail-fetch threads hitting a block around the same time share one rotation
  instead of each triggering their own.

- **[matcher.py](matcher.py)** — pure string-matching logic, no I/O. Keywords are
  comma-separated AND-groups; within a group, `OR`-separated alternatives (e.g.
  `keychron OR key chron, k3` = `(keychron OR key chron) AND k3`). `title_satisfies()`
  checks title alone and returns `True`/`False`/`None` (`None` = inconclusive, needs the
  description). `full_satisfies()` checks title+description together. This
  title-first/description-second split is what lets [app.py](app.py) skip a description
  fetch whenever the title alone settles the match.

- **[categories.py](categories.py)** — fetches Vinted's category tree (id/title/path) for
  the category picker. No documented `/catalogs` endpoint exists, so this scrapes the
  homepage HTML for a Next.js RSC streaming chunk containing `catalogTree` JSON and
  flattens it. Cached in-memory for 24h.

- **[app.py](app.py)** — Flask routes + orchestration. `run_search()` is the core loop:
  page through Vinted search results, resolve what title alone can settle, batch-fetch
  descriptions for the rest concurrently, then resolve seller country only for items that
  made the final cut (not every scanned candidate). A single shared `VintedScraper` keeps
  session cookies warm across requests; `_search_lock` serializes searches so concurrent
  requests can't interleave on that shared session or double Vinted's view of this
  instance's request rate. Also serves `/api/version` and `/api/update`, which `git pull`
  the repo in place and exit the process (the Docker restart policy brings it back up on
  the new code) — this is how the frontend's self-update button works; both require
  `GITHUB_TOKEN` since the repo is private.

- **[templates/index.html](templates/index.html)** — single-page frontend, vanilla JS, no
  build step. Client-side keyword splitting here (for highlighting matched terms in
  results) must stay in sync with `matcher.py`'s AND/OR parsing.

### Request flow

Browser → `/api/search` → `run_search()` → `scraper.search()` (candidate pool from
Vinted, sorted/paginated) → `matcher.title_satisfies()` per candidate → for inconclusive
ones, `scraper.get_item_descriptions()` (concurrent) → `matcher.full_satisfies()` →
`scraper.get_user_countries()` for final results only → JSON response.

### Key constraint

Vinted actively blocks scraping (403). Anything touching request headers, timing, or the
device/session identifiers in `scraper.py` needs to preserve the "looks like the real iOS
app" behavior — throttling (`MIN_API_DELAY`), session refresh cadence
(`SESSION_REFRESH_INTERVAL`), and the block-triggered VPN rotation (`vpn.py`) all exist
because of this.
