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

BLOCK_WAIT_SECONDS = int(os.environ.get("BLOCK_WAIT_SECONDS", 1800))  # 30 min default
MIN_API_DELAY = float(os.environ.get("MIN_API_DELAY", 1.5))           # seconds between catalog search requests
VINTED_PROXY = os.environ.get("VINTED_PROXY", "")                     # proxy in host:port format (optional)

# Item-description fetches (one per candidate that title alone can't resolve)
# run concurrently instead of one-at-a-time — each is a ~1-2MB page fetch, so
# doing them sequentially at MIN_API_DELAY spacing is what makes a search feel
# stuck. DETAIL_CONCURRENCY bounds how many run in flight at once; each still
# gets a small random jitter before it fires so they don't all launch at once.
DETAIL_CONCURRENCY = int(os.environ.get("DETAIL_CONCURRENCY", 5))
DETAIL_JITTER_SECONDS = float(os.environ.get("DETAIL_JITTER_SECONDS", 0.3))

SESSION_REFRESH_INTERVAL = 50  # refresh session every N API calls

# ── Deep-search bounds ───────────────────────────────────────────────────────
# A title-only match needs no extra request. Anything else needs a per-item
# description fetch, which is what makes this slower (but correct) than
# Vinted's own search. These caps keep a single search request bounded.
DEFAULT_MAX_RESULTS = 40
MAX_MAX_RESULTS = 100
DEFAULT_MAX_SCAN = 200
MAX_MAX_SCAN = 500

DESCRIPTION_CACHE_SIZE = 2000
