"""SentiBot v1 configuration. All tunables in one place."""

VERSION = "0.1.0-dry"

# Discovery window (entry gates)
TTR_MIN_HOURS = 24
TTR_MAX_HOURS = 70
PRICE_MIN = 0.85
PRICE_MAX = 0.95

# Loop cadences (seconds)
DISCOVERY_INTERVAL_S = 60
PRICE_UPDATE_INTERVAL_S = 30
FINAL_HOUR_INTERVAL_S = 5
FINAL_HOUR_THRESHOLD_S = 3600
RESOLUTION_CHECK_INTERVAL_S = 60
HEARTBEAT_INTERVAL_S = 30

# Paper position sizing
SIMULATED_POSITION_USD = 1.0
ENTRY_SLIPPAGE = 0.005
EXIT_SLIPPAGE = 0.005
MAX_FILL_PRICE = 0.99  # cap simulated fill so we never compute negative shares

# API endpoints
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

# HTTP
HTTP_TIMEOUT_S = 15
HTTP_RETRIES = 2

# State paths
STATE_DIR = "state"
POSITIONS_CSV = "state/sentibot_positions.csv"
DISCOVERY_CSV = "state/sentibot_discovery.csv"
PRICELOG_CSV = "state/sentibot_pricelog.csv"
RESOLUTIONS_CSV = "state/sentibot_resolutions.csv"
HEARTBEAT_FILE = "state/heartbeat.txt"

# Resolution verification
RESOLUTION_MIN_AGE_S = 3600           # market must be past end_date by ≥1hr
RESOLUTION_CONFIRM_DELAY_S = 60       # 2nd poll runs 60s after 1st
RESOLUTION_WINNER_THRESHOLD = 0.99    # outcome price must be ≥ this
RESOLUTION_PRICE_SUM_TOLERANCE = 0.02 # |yes+no - 1.0| must be ≤ this

# Logging
LOG_LEVEL = "INFO"

# Dashboard
DASHBOARD_PORT_DEFAULT = 8080  # Railway overrides via $PORT
SCALED_VIEW_MULTIPLIER = 100.0  # for "show as if $100/trade" toggle
