# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Vinted Better Search — a Flask app that wraps Vinted's search API with strict, literal
keyword matching. Vinted's own search is fuzzy and frequently ignores the words a user
types (and its `svc-catalogue/items` API doesn't return descriptions at all), so this app
re-verifies every candidate itself. It does so in two stages: a fast **quick search** that
matches only the listing fields already in the search response (title + brand, zero
per-item requests), then an opt-in **Deep Search** that fetches the item page
(description) and seller country for just the items the user selected and re-checks them.
Quick search can optionally also list **partial matches** — items whose title/brand
satisfied only some keyword groups, which Deep Search then settles against the description.

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
  `keychron OR key chron, k3` = `(keychron OR key chron) AND k3`). `listing_satisfies()`
  is the strict, listing-only match that powers quick search: `True` only if every include
  group is present in the title+brand text and no exclude term is, with no inconclusive
  third answer. `full_satisfies()` checks title+description together and powers Deep
  Search. `first_exclude_hit()` reports *which* exclude term rejected an item, so a card
  can say "Excluded: <term>" instead of just dimming. `missing_groups()` returns the
  include groups with no alternative in a text — quick search uses it to tell a *partial*
  candidate (some groups matched) from Vinted's fuzzy noise (none did), and to report
  which keywords a partial result is missing. (`title_satisfies()` remains for the legacy
  title-first/description-second split.)

- **[categories.py](categories.py)** — fetches Vinted's category tree (id/title/path) for
  the category picker. No documented `/catalogs` endpoint exists, so this scrapes the
  homepage HTML for a Next.js RSC streaming chunk containing `catalogTree` JSON and
  flattens it. Cached in-memory for 24h.

- **[app.py](app.py)** — Flask routes + orchestration, two stages. `run_search()` (stage 1)
  pages through *all* of Vinted's search results (until its `pagination.total_pages` is
  reached, an empty page ends the run, or the `MAX_SEARCH_PAGES` safety ceiling trips, which
  the response reports as `hit_page_limit` + `pages`) and matches each candidate on listing
  fields only (`listing_satisfies` over title+brand) — it never fetches an item page or a
  seller profile, so `requests_sent` is just the number of search pages. It keeps each
  result's `seller_id` (previously popped) because Deep Search needs it. With `show_partial`
  (the `partial=1` query arg), it also collects candidates matching only some include groups
  as `partial` results carrying the groups they're `missing` — but never when no group
  matched at all. Full matches are listed first, then partials (no cap — both are bounded by
  pages scanned); the response reports `partial_count` and the `deep_search_max_items` cap
  so the frontend can stop over-selection before the server would 400.
  `run_deep_search()`
  (stage 2, `POST /api/deep-search`) takes the selected items plus the keywords of the
  search that produced them, batch-fetches descriptions (`get_item_descriptions`) and
  seller countries (`get_user_countries`) for just those items, and returns a per-item
  verdict — `match` / `excluded` (with the offending term) / `unverified` (fetch failed,
  retryable) / `no_match`. A single shared `VintedScraper` keeps session cookies warm
  across requests; `_search_lock` serializes searches so concurrent requests can't
  interleave on that shared session or double Vinted's view of this instance's request
  rate. Both flows share `/api/search/progress`. Also serves `/api/version` and
  `/api/update`, which `git pull` the repo in place and exit the process (the Docker
  restart policy brings it back up on the new code) — this is how the frontend's
  self-update button works; both require `GITHUB_TOKEN` since the repo is private.

- **[templates/index.html](templates/index.html)** — single-page frontend, vanilla JS, no
  build step. Each result card has a checkbox; "Run Deep Search" POSTs the checked items to
  `/api/deep-search` and updates those cards in place with verdicts (matched items get the
  "Verified" stamp + description snippet, excluded ones dim with a reason badge, failed
  ones stay checked for retry). A "Show partial matches" toggle (`partial=1`) renders full
  matches first and then, under a "Partial matches" divider, partial cards badged
  "Partial — missing: <groups>"; a partial Deep-Searched to `match` becomes a normal
  verified card, and a `no_match` one dims and untick like an excluded card. Because a
  broad query can now return more results than one Deep Search may fetch, the button
  disables (showing "(max N)") once the selection exceeds `deep_search_max_items`, and
  "Select all" ticks only the first N not-yet-verified results. Client-side
  keyword splitting here (for highlighting matched terms in results) must stay in sync with
  `matcher.py`'s AND/OR parsing.

### Request flow

Browser → `/api/search` → `run_search()` → `scraper.search()` (candidate pool from Vinted,
page 1..N) → `matcher.listing_satisfies()` per candidate (title + brand only), plus
`matcher.missing_groups()` for partials when `partial=1` → JSON response. No per-item
requests.

Then, for the items the user checks: Browser → `/api/deep-search` →
`run_deep_search()` → `scraper.get_item_descriptions()` (concurrent) →
`matcher.full_satisfies()` / `first_exclude_hit()` per item → `scraper.get_user_countries()`
for those items' sellers → per-item verdicts → JSON response → cards updated in place.

### Key constraint

Vinted actively blocks scraping (403). Anything touching request headers, timing, or the
device/session identifiers in `scraper.py` needs to preserve the "looks like the real iOS
app" behavior — throttling (`MIN_API_DELAY`), session refresh cadence
(`SESSION_REFRESH_INTERVAL`), and the block-triggered VPN rotation (`vpn.py`) all exist
because of this.
