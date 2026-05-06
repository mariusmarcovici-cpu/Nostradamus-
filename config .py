"""Nostradamus / SentiBot configuration. All tunables in one place.

v0.2.0-dry changes (May 6, 2026):
  - PRICE_MIN raised 0.85 → 0.93 (math: at 0.85 entry, breakeven needs 85% WR;
    bot was delivering 84.6% so was structurally negative)
  - SKIP_CLUSTERS added — sports cluster blocked by default. All 4 catastrophic
    losses + 4 manual sells in v0.1.0 data were sports markets (DOTA2/LoL).
  - MAX_OPEN_POSITIONS = 120 — cap so the system stays tractable for review.
  - AUTO_RESOLVE_AS_NO_AFTER_HOURS = 24 — eliminates the human-verification
    bottleneck. Stuck positions auto-resolve as NO (the dominant outcome at
    these prices) once they age past this threshold.
  - SINGLE-POLL resolution by default (was 2-poll-must-match). No more
    RESOLUTION_DISPUTED states unless explicitly re-enabled.
"""

import os


def _env(key: str, default, cast=str):
    """Read env var with type cast. Returns default on missing/invalid."""
    v = os.environ.get(key, "")
    if v is None or v == "":
        return default
    try:
        return cast(v)
    except (ValueError, TypeError):
        return default


VERSION = "0.2.0-dry"

# Discovery window (entry gates)
TTR_MIN_HOURS = _env("TTR_MIN_HOURS", 24, int)
TTR_MAX_HOURS = _env("TTR_MAX_HOURS", 70, int)
PRICE_MIN     = _env("PRICE_MIN", 0.93, float)
PRICE_MAX     = _env("PRICE_MAX", 0.95, float)

# v0.2.0: cluster skip list. Comma-separated cluster CODES (e.g. "A_SPORTS,B_MENTION").
# Default skips sports — see cluster.py for codes.
_skip_raw = os.environ.get("SKIP_CLUSTERS", "A_SPORTS").strip()
SKIP_CLUSTERS = {c.strip() for c in _skip_raw.split(",") if c.strip()}

# v0.2.0: max simultaneous open positions. Discovery skips creating new
# entries once this is reached. Existing positions still get price updates
# and resolve normally — only entry is gated.
MAX_OPEN_POSITIONS = _env("MAX_OPEN_POSITIONS", 120, int)

# v0.2.0: auto-resolve stuck positions as NO after this many hours past
# end_date. Eliminates the human-verification bottleneck. Set to a large
# number (e.g. 99999) to disable (positions then sit forever).
AUTO_RESOLVE_AS_NO_AFTER_HOURS = _env("AUTO_RESOLVE_AS_NO_AFTER_HOURS", 24, int)

# Loop cadences (seconds)
DISCOVERY_INTERVAL_S = _env("DISCOVERY_INTERVAL_S", 60, int)
PRICE_UPDATE_INTERVAL_S = _env("PRICE_UPDATE_INTERVAL_S", 30, int)
FINAL_HOUR_INTERVAL_S = 5
FINAL_HOUR_THRESHOLD_S = 3600
RESOLUTION_CHECK_INTERVAL_S = _env("RESOLUTION_CHECK_INTERVAL_S", 60, int)
HEARTBEAT_INTERVAL_S = 30

# Paper position sizing
SIMULATED_POSITION_USD = _env("SIMULATED_POSITION_USD", 1.0, float)
ENTRY_SLIPPAGE = 0.005
EXIT_SLIPPAGE = 0.005
MAX_FILL_PRICE = 0.99

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
RESOLUTION_MIN_AGE_S = 3600
RESOLUTION_CONFIRM_DELAY_S = 60       # legacy: kept for back-compat (unused in single-poll mode)
RESOLUTION_WINNER_THRESHOLD = 0.99
RESOLUTION_PRICE_SUM_TOLERANCE = 0.02

# v0.2.0: SINGLE-POLL resolution. Was 2-poll-must-match in v0.1.0; that
# created RESOLUTION_DISPUTED states that needed human review. In single-poll
# mode, a clear winner with token alignment OK is auto-resolved on the
# first poll. Set RESOLUTION_REQUIRE_TWO_POLLS=true to restore old behavior.
RESOLUTION_REQUIRE_TWO_POLLS = _env("RESOLUTION_REQUIRE_TWO_POLLS", "false", str).lower() in ("true", "1", "yes")

# v0.2.0: BLOCK UMA-flagged markets at entry. Was non-blocking in v0.1.0
# (just labeled). UMA-resolved markets are subjective + dispute-prone; on
# the v0.1.0 dataset all 4 catastrophic losses + 4 manual-sells carried
# the UMA flag. Default-on. Set to false to restore old "label-only" mode.
BLOCK_UMA_FLAGGED = _env("BLOCK_UMA_FLAGGED", "true", str).lower() in ("true", "1", "yes")

# Logging
LOG_LEVEL = _env("LOG_LEVEL", "INFO", str)

# Dashboard
DASHBOARD_PORT_DEFAULT = 8080
SCALED_VIEW_MULTIPLIER = 100.0
