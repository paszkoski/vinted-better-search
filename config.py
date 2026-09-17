# Vinted Better Search — Configuration
import os

# Vinted domain to search (use your country's domain)
VINTED_BASE_URL = os.environ.get("VINTED_BASE_URL", "https://www.vinted.pl")
VINTED_API_URL = f"{VINTED_BASE_URL}/api/v2"
# Catalog search moved off /api/v2 to its own "api." host + service path
# (Vinted migrated it server-side; see scraper.py's search()).
VINTED_CATALOG_URL = "https://api." + VINTED_BASE_URL.split("://", 1)[1].removeprefix("www.") + "/svc-catalogue"

DEFAULT_CURRENCY = "PLN"
DEFAULT_ORDER = "price_low_to_high"
CATALOG_PER_PAGE = 96  # Vinted's server-side max for catalog/items

BLOCK_WAIT_SECONDS = int(os.environ.get("BLOCK_WAIT_SECONDS", 1800))  # 30 min default — fallback cooldown
                                                                        # only used when a VPN rotation attempt
                                                                        # itself fails (see vpn.py)
MIN_API_DELAY = float(os.environ.get("MIN_API_DELAY", 1.5))           # seconds between catalog search requests

# NordVPN countries to rotate between on a block (comma-separated, must match
# nordvpn_switcher's bundled country list, e.g. "Poland,Germany,Netherlands").
# Empty = rotate across every available NordVPN server ("complete rotation"),
# which depends on NordVPN's public server-list endpoint being reachable.
NORDVPN_COUNTRIES = os.environ.get("NORDVPN_COUNTRIES", "Poland,Germany,Netherlands,Czech Republic")
# Minimum seconds between two VPN rotations — coalesces simultaneous blocks
# from concurrent detail-fetch threads into a single rotation instead of
# rotating once per thread.
VPN_ROTATE_COOLDOWN_SECONDS = float(os.environ.get("VPN_ROTATE_COOLDOWN_SECONDS", 15))
# How many times a single blocked request will hop to a new VPN server before
# giving up and falling back to the BLOCK_WAIT_SECONDS cooldown. Bounded so a
# broad block of NordVPN's IP ranges (not just one server) can't hang a
# search forever hammering NordVPN with rotation attempts.
VPN_MAX_ROTATE_ATTEMPTS = int(os.environ.get("VPN_MAX_ROTATE_ATTEMPTS", 15))

# Item-description fetches (one per candidate that title alone can't resolve)
# run concurrently instead of one-at-a-time — each is a ~1-2MB page fetch, so
# doing them sequentially at MIN_API_DELAY spacing is what makes a search feel
# stuck. DETAIL_CONCURRENCY bounds how many run in flight at once; each still
# gets a small random jitter before it fires so they don't all launch at once.
DETAIL_CONCURRENCY = int(os.environ.get("DETAIL_CONCURRENCY", 5))
DETAIL_JITTER_SECONDS = float(os.environ.get("DETAIL_JITTER_SECONDS", 0.3))
# Minimum spacing enforced between successive detail-fetch dispatches, on top
# of the random jitter above — without it, DETAIL_CONCURRENCY workers can all
# fire within the same jitter window, which looks automated on its own.
DETAIL_MIN_GAP_SECONDS = float(os.environ.get("DETAIL_MIN_GAP_SECONDS", 0.5))
# Empirically, Vinted's 429 limit on detail-page fetches is a hard per-window
# request-count threshold, not burst/velocity detection — full speed up to
# DETAIL_BATCH_SIZE requests, then pausing DETAIL_BATCH_COOLDOWN_SECONDS
# clears it. So detail fetches are paced in batches rather than a smooth
# trickle: fire freely (bounded by DETAIL_CONCURRENCY/DETAIL_MIN_GAP_SECONDS)
# until the batch size is hit, then every thread pauses together before the
# next batch starts.
DETAIL_BATCH_SIZE = int(os.environ.get("DETAIL_BATCH_SIZE", 29))
DETAIL_BATCH_COOLDOWN_SECONDS = float(os.environ.get("DETAIL_BATCH_COOLDOWN_SECONDS", 10))

SESSION_REFRESH_INTERVAL = 50  # refresh session every N API calls

# ── Deep-search bounds ───────────────────────────────────────────────────────
# A title-only match needs no extra request. Anything else needs a per-item
# description fetch, which is what makes this slower (but correct) than
# Vinted's own search. These are just defaults when the caller doesn't
# specify max_results/max_scan — no upper cap is enforced beyond that.
DEFAULT_MAX_RESULTS = 40
DEFAULT_MAX_SCAN = 200

DESCRIPTION_CACHE_SIZE = 2000
