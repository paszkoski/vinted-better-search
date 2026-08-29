# Vinted Better Search — Configuration
import os

# Vinted domain to search (use your country's domain)
VINTED_BASE_URL = os.environ.get("VINTED_BASE_URL", "https://www.vinted.pl")
VINTED_API_URL = f"{VINTED_BASE_URL}/api/v2"

DEFAULT_CURRENCY = "PLN"
DEFAULT_ORDER = "relevance"
CATALOG_PER_PAGE = 96  # Vinted's server-side max for catalog/items

BLOCK_WAIT_SECONDS = int(os.environ.get("BLOCK_WAIT_SECONDS", 1800))  # 30 min default
MIN_API_DELAY = float(os.environ.get("MIN_API_DELAY", 1.5))           # seconds between catalog search requests
MIN_DETAIL_DELAY = float(os.environ.get("MIN_DETAIL_DELAY", 0.6))     # seconds between item-detail page fetches
VINTED_PROXY = os.environ.get("VINTED_PROXY", "")                     # proxy in host:port format (optional)

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
