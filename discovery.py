"""Discovery pipeline: poll Gamma, filter, return entry candidates.

Filter chain (cheapest first):
  1. Structural (binary, open, has orderbook, in TTR window)
  2. Crypto blacklist (slug + tag + regex)
  3. UMA dispute flag (non-blocking, just labels)
  4. Price band check (CLOB call — most expensive)
  5. Already-entered check (CSV lookup)
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests

import config
from blacklists import is_crypto, has_uma_dispute_marker
from cluster import classify

log = logging.getLogger(__name__)


# ---------- HTTP ----------

def _get(url: str, params: Optional[dict] = None) -> Optional[dict]:
    """GET with retry. Returns parsed JSON or None on failure."""
    for attempt in range(config.HTTP_RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=config.HTTP_TIMEOUT_S)
            if r.status_code == 200:
                return r.json()
            log.warning("GET %s -> %d (attempt %d)", url, r.status_code, attempt + 1)
        except Exception as e:
            log.warning("GET %s exception: %s (attempt %d)", url, e, attempt + 1)
        time.sleep(0.5 * (attempt + 1))
    return None


# ---------- Gamma API ----------

def fetch_markets_in_window(now_utc: datetime) -> list:
    """Fetch all open binary markets with end_date in [now+TTR_MIN, now+TTR_MAX].

    Polymarket Gamma `/markets` accepts end_date_min/end_date_max as ISO strings.
    We paginate via offset until the page is empty or under the limit.
    """
    end_min = now_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    # We fetch a bit wider than TTR_MAX so we can re-evaluate borderline markets
    # over time as they drift into band.
    from datetime import timedelta
    end_min_dt = now_utc + timedelta(hours=config.TTR_MIN_HOURS - 1)
    end_max_dt = now_utc + timedelta(hours=config.TTR_MAX_HOURS + 1)
    end_min = end_min_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    end_max = end_max_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    url = f"{config.GAMMA_BASE}/markets"
    params = {
        "closed": "false",
        "active": "true",
        "end_date_min": end_min,
        "end_date_max": end_max,
        "limit": 500,
        "offset": 0,
    }
    out = []
    while True:
        data = _get(url, params=params)
        if not data:
            break
        # Gamma sometimes returns a list, sometimes {"data": [...]}
        page = data if isinstance(data, list) else data.get("data", [])
        if not page:
            break
        out.extend(page)
        if len(page) < params["limit"]:
            break
        params["offset"] += params["limit"]
        if params["offset"] > 5000:
            log.warning("Discovery offset exceeded 5000, breaking")
            break
    return out


def fetch_market_by_id(market_id: str) -> Optional[dict]:
    """Used for resolution checks. Gamma /markets/{id}."""
    url = f"{config.GAMMA_BASE}/markets/{market_id}"
    return _get(url)


# ---------- CLOB API ----------

def fetch_orderbook(token_id: str) -> Optional[dict]:
    """CLOB /book?token_id=... returns {asks: [{price, size}], bids: [...]}.

    asks are sorted ascending by price; bids descending.
    """
    url = f"{config.CLOB_BASE}/book"
    return _get(url, params={"token_id": token_id})


def best_ask_and_bid(book: dict) -> tuple:
    """Return (best_ask, best_bid) as floats, or (None, None) if empty."""
    if not book:
        return None, None
    asks = book.get("asks") or []
    bids = book.get("bids") or []
    # Polymarket CLOB returns prices as strings; asks ascending, bids descending
    try:
        # Some endpoints return asks descending; sort defensively
        ask_prices = sorted(float(a["price"]) for a in asks if "price" in a)
        bid_prices = sorted((float(b["price"]) for b in bids if "price" in b), reverse=True)
        best_ask = ask_prices[0] if ask_prices else None
        best_bid = bid_prices[0] if bid_prices else None
        return best_ask, best_bid
    except (ValueError, KeyError, TypeError) as e:
        log.warning("Bad book payload: %s", e)
        return None, None


def book_depth_top3(book: dict, side: str = "asks") -> float:
    """Sum of size in the top 3 levels of the given side. Useful as a depth proxy."""
    if not book:
        return 0.0
    levels = book.get(side) or []
    try:
        sizes = []
        for lvl in levels[:3]:
            if "size" in lvl:
                sizes.append(float(lvl["size"]))
        return sum(sizes)
    except (ValueError, TypeError):
        return 0.0


# ---------- Outcome / token-id parsing ----------

def parse_outcomes_and_tokens(market: dict) -> Optional[dict]:
    """Extract {yes_token_id, no_token_id} from a Gamma market dict.

    Gamma returns `outcomes` and `clobTokenIds` as JSON-encoded strings.
    Index alignment: outcomes[i] corresponds to clobTokenIds[i].
    We find the index of "No" (case-insensitive) and use its token id.
    """
    try:
        outcomes_raw = market.get("outcomes")
        tokens_raw = market.get("clobTokenIds")
        if not outcomes_raw or not tokens_raw:
            return None
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
        if len(outcomes) != 2 or len(tokens) != 2:
            return None
        no_idx = None
        yes_idx = None
        for i, o in enumerate(outcomes):
            ol = (o or "").strip().lower()
            if ol == "no":
                no_idx = i
            elif ol == "yes":
                yes_idx = i
        if no_idx is None or yes_idx is None:
            return None
        return {
            "yes_token_id": str(tokens[yes_idx]),
            "no_token_id": str(tokens[no_idx]),
        }
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        log.debug("parse_outcomes failed: %s", e)
        return None


def parse_end_date(market: dict) -> Optional[datetime]:
    """Parse end_date_iso to UTC datetime."""
    raw = market.get("endDate") or market.get("end_date_iso") or market.get("end_date")
    if not raw:
        return None
    try:
        # Handle "Z" suffix and offset-aware
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


# ---------- Filter chain ----------

def evaluate_market(market: dict, now_utc: datetime,
                    already_entered_ids: set) -> dict:
    """Return a dict describing the evaluation outcome.

    Returned shape:
        {
            "pass": bool,
            "reject_reason": str | None,
            "row": {...full row to log to discovery.csv...},
            "entry_payload": {...if pass, payload for position.create_entry...} | None,
        }
    """
    market_id = str(market.get("id") or market.get("conditionId") or "")
    slug = market.get("slug") or ""
    question = market.get("question") or ""
    tags = market.get("tags") or []
    category = market.get("category") or ""
    end_dt = parse_end_date(market)
    ttr_hours = None
    if end_dt:
        ttr_hours = (end_dt - now_utc).total_seconds() / 3600.0

    base_row = {
        "discovery_ts": now_utc.isoformat(),
        "market_id": market_id,
        "slug": slug,
        "end_date_iso": end_dt.isoformat() if end_dt else "",
        "ttr_hours": round(ttr_hours, 2) if ttr_hours is not None else "",
        "no_best_ask": "",
        "volume": market.get("volume") or "",
        "cluster": classify(question, tags, category),
        "pass_fail": "FAIL",
        "reject_reason": "",
    }

    # 1. Structural
    if not market_id:
        base_row["reject_reason"] = "no_market_id"
        return {"pass": False, "reject_reason": base_row["reject_reason"], "row": base_row, "entry_payload": None}
    if market.get("closed") or market.get("archived") or market.get("resolved"):
        base_row["reject_reason"] = "closed_or_archived"
        return {"pass": False, "reject_reason": base_row["reject_reason"], "row": base_row, "entry_payload": None}
    if not market.get("enableOrderBook", True):
        base_row["reject_reason"] = "no_orderbook"
        return {"pass": False, "reject_reason": base_row["reject_reason"], "row": base_row, "entry_payload": None}
    if ttr_hours is None:
        base_row["reject_reason"] = "no_end_date"
        return {"pass": False, "reject_reason": base_row["reject_reason"], "row": base_row, "entry_payload": None}
    if ttr_hours < config.TTR_MIN_HOURS or ttr_hours > config.TTR_MAX_HOURS:
        base_row["reject_reason"] = f"ttr_out_of_window_{ttr_hours:.1f}h"
        return {"pass": False, "reject_reason": base_row["reject_reason"], "row": base_row, "entry_payload": None}

    # 2. Crypto blacklist
    if is_crypto(slug, tags, question):
        base_row["reject_reason"] = "crypto_blacklist"
        return {"pass": False, "reject_reason": base_row["reject_reason"], "row": base_row, "entry_payload": None}

    # v0.2.0: 2b. Cluster skip list (default: A_SPORTS).
    # Sports markets had 100% of catastrophic losses in v0.1.0 data.
    if base_row["cluster"] in config.SKIP_CLUSTERS:
        base_row["reject_reason"] = f"cluster_skipped:{base_row['cluster']}"
        return {"pass": False, "reject_reason": base_row["reject_reason"], "row": base_row, "entry_payload": None}

    # 3. UMA dispute flag (non-blocking)
    uma_flag = has_uma_dispute_marker(
        market.get("resolutionSource") or "",
        market.get("description") or "",
    )

    # 4. Token IDs
    tokens = parse_outcomes_and_tokens(market)
    if not tokens:
        base_row["reject_reason"] = "bad_outcomes_or_tokens"
        return {"pass": False, "reject_reason": base_row["reject_reason"], "row": base_row, "entry_payload": None}

    # 5. Already entered
    if market_id in already_entered_ids:
        base_row["reject_reason"] = "already_entered"
        return {"pass": False, "reject_reason": base_row["reject_reason"], "row": base_row, "entry_payload": None}

    # 6. Price band — CLOB call (most expensive, last)
    book = fetch_orderbook(tokens["no_token_id"])
    best_ask, best_bid = best_ask_and_bid(book)
    if best_ask is None:
        base_row["reject_reason"] = "empty_or_no_book"
        return {"pass": False, "reject_reason": base_row["reject_reason"], "row": base_row, "entry_payload": None}
    base_row["no_best_ask"] = round(best_ask, 4)
    if not (config.PRICE_MIN <= best_ask <= config.PRICE_MAX):
        base_row["reject_reason"] = f"price_out_of_band_{best_ask:.3f}"
        return {"pass": False, "reject_reason": base_row["reject_reason"], "row": base_row, "entry_payload": None}

    # PASS
    base_row["pass_fail"] = "PASS"
    depth = book_depth_top3(book, side="asks")

    # Pull event slug — Polymarket renders /event/<eventSlug>, not /event/<marketSlug>
    event_slug = ""
    events = market.get("events") or []
    if events and isinstance(events, list):
        first_event = events[0] if events[0] else {}
        event_slug = first_event.get("slug") or ""
    # Fallbacks if events array isn't populated
    if not event_slug:
        event_slug = market.get("eventSlug") or market.get("event_slug") or ""

    # conditionId is the unambiguous market identifier; some Polymarket URLs accept it
    condition_id = market.get("conditionId") or ""

    entry_payload = {
        "market_id": market_id,
        "slug": slug,
        "event_slug": event_slug,
        "condition_id": condition_id,
        "question": question,
        "cluster_auto": base_row["cluster"],
        "ttr_at_entry_hours": round(ttr_hours, 2),
        "no_token_id": tokens["no_token_id"],
        "yes_token_id": tokens["yes_token_id"],
        "no_best_ask_at_entry": round(best_ask, 4),
        "no_best_bid_at_entry": round(best_bid, 4) if best_bid is not None else "",
        "volume_at_entry": market.get("volume") or "",
        "book_depth_no_top3": round(depth, 2),
        "uma_risk_flag": int(uma_flag),
        "end_date_iso": end_dt.isoformat() if end_dt else "",
    }
    return {"pass": True, "reject_reason": None, "row": base_row, "entry_payload": entry_payload}
