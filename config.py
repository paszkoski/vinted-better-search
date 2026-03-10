# Vinted Scraper Configuration
import os

# Vinted domain to scrape (use your country's domain)
VINTED_BASE_URL = os.environ.get("VINTED_BASE_URL", "https://www.vinted.pl")
VINTED_API_URL = f"{VINTED_BASE_URL}/api/v2"

# Default search parameters
DEFAULT_SEARCH_QUERY = "Xreal one pro"
DEFAULT_CURRENCY = "PLN"
DEFAULT_ORDER = "relevance"
DEFAULT_PER_PAGE = 24
DEFAULT_CATALOG_ID = None  # Set to an integer category ID to filter, e.g. 2994 for Elektronika
DEFAULT_PRICE_FROM = None  # Minimum price filter (e.g. 100)
DEFAULT_PRICE_TO = None    # Maximum price filter (e.g. 500)

# Monitor mode
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
POLL_INTERVAL = 300  # seconds (5 minutes)
SESSION_REFRESH_INTERVAL = 50   # refresh session every N API calls
BLOCK_WAIT_SECONDS = int(os.environ.get("BLOCK_WAIT_SECONDS", 1800))  # 30 min default
MIN_API_DELAY = float(os.environ.get("MIN_API_DELAY", 1.5))           # seconds between requests
VINTED_PROXY = os.environ.get("VINTED_PROXY", "")                     # proxy in host:port format (optional)

# ── Notifications ──────────────────────────────────────────────────────────────

# n8n webhook — sends full item JSON payload to your n8n workflow
NOTIFY_N8N_ENABLED = False
N8N_WEBHOOK_URL = os.environ.get("N8N_WEBHOOK_URL", "")  # e.g. "https://your-n8n.com/webhook/abc123"

# Pushover (future)
NOTIFY_PUSHOVER_ENABLED = False
# PUSHOVER_TOKEN = ""
# PUSHOVER_USER = ""
