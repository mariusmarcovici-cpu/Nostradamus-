"""Paper position lifecycle.

CSV-only persistence to survive Railway redeploys. File-locked writes for
manual-sell race safety. NaN-prevention via assert helper.
"""

import csv
import logging
import os
import time
from datetime import datetime, timezone
from threading import Lock
from typing import Optional

import config

log = logging.getLogger(__name__)

# Process-wide lock to serialize all CSV writes.
# Multi-process deploys would need fcntl; Railway is single-process so this is enough.
_LOCK = Lock()

POSITION_COLS = [
    "entry_ts", "market_id", "slug", "event_slug", "condition_id", "question",
    "cluster_auto", "cluster_override",
    "ttr_at_entry_hours",
    "no_token_id", "yes_token_id",
    "no_best_ask_at_entry", "no_best_bid_at_entry",
    "simulated_fill_price", "simulated_shares",
    "volume_at_entry", "book_depth_no_top3", "uma_risk_flag",
    "end_date_iso",
    "status",
    "exit_ts", "exit_price", "exit_reason", "pnl",
    "current_no_ask", "current_no_bid", "last_price_ts",
    # Resolution verification audit fields
    "verify_first_poll_ts", "verify_first_poll_winner",
    "verify_last_attempt_ts", "verify_attempt_count",
    "polymarket_url",
]

DISCOVERY_COLS = [
    "discovery_ts", "market_id", "slug", "end_date_iso", "ttr_hours",
    "no_best_ask", "volume", "cluster", "pass_fail", "reject_reason",
]

PRICELOG_COLS = [
    "ts", "market_id", "no_best_ask", "no_best_bid", "ttr_remaining_hours",
]

RESOLUTION_COLS = [
    "ts", "market_id", "slug", "gamma_closed", "outcomes_raw", "prices_raw",
    "parsed_winner", "token_alignment_ok", "age_since_end_min",
    "poll_number", "prior_winner", "action_taken", "notes",
]

# Status values:
#   OPEN
#   RESOLUTION_PENDING_VERIFY  - past end_date but doesn't yet pass verification gates
#   RESOLUTION_AWAITING_2ND    - poll #1 passed; waiting on confirming poll #2
#   RESOLUTION_DISPUTED        - 2 polls returned different winners; manual review required
#   UMA_PENDING                - past end + 4hr, market still not closed on Gamma
#   RESOLVED_NO                - immutable, NO won
#   RESOLVED_YES               - immutable, YES won
#   MANUAL_SOLD                - early exit via dashboard
#   MANUAL_RESOLVED_NO         - human-confirmed NO win after auto-verify failed
#   MANUAL_RESOLVED_YES        - human-confirmed YES win after auto-verify failed

REQUIRED_ON_ENTRY = [
    "entry_ts", "market_id", "no_token_id",
    "simulated_fill_price", "simulated_shares", "status",
]


# ---------- CSV helpers ----------

def _ensure_csv(path: str, cols: list):
    """Create CSV with header if missing."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=cols).writeheader()


def init_state():
    _ensure_csv(config.POSITIONS_CSV, POSITION_COLS)
    _ensure_csv(config.DISCOVERY_CSV, DISCOVERY_COLS)
    _ensure_csv(config.PRICELOG_CSV, PRICELOG_COLS)
    _ensure_csv(config.RESOLUTIONS_CSV, RESOLUTION_COLS)


def _assert_no_nan(row: dict, required: list):
    """Iron-rule guard against silent NaN/None in required columns."""
    for col in required:
        v = row.get(col)
        if v is None or v == "" or (isinstance(v, float) and v != v):
            raise ValueError(f"Required col '{col}' is empty/NaN in row: {row}")


def _write_row(path: str, cols: list, row: dict):
    with _LOCK:
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            # Fill missing cols with empty string for stable schema
            full = {c: row.get(c, "") for c in cols}
            w.writerow(full)


def _read_all(path: str, cols: list) -> list:
    if not os.path.exists(path):
        return []
    with _LOCK:
        with open(path, "r", newline="") as f:
            return list(csv.DictReader(f))


def _rewrite_all(path: str, cols: list, rows: list):
    with _LOCK:
        tmp = path + ".tmp"
        with open(tmp, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, "") for c in cols})
        os.replace(tmp, path)


# ---------- Public API ----------

def all_entered_market_ids() -> set:
    """Set of market_ids that have ever been entered (HR5: one entry per market, ever)."""
    rows = _read_all(config.POSITIONS_CSV, POSITION_COLS)
    return {r["market_id"] for r in rows if r.get("market_id")}


def open_positions() -> list:
    rows = _read_all(config.POSITIONS_CSV, POSITION_COLS)
    return [r for r in rows if r.get("status") == "OPEN"]


def all_positions() -> list:
    return _read_all(config.POSITIONS_CSV, POSITION_COLS)


def find_position(market_id: str) -> Optional[dict]:
    for r in _read_all(config.POSITIONS_CSV, POSITION_COLS):
        if r.get("market_id") == market_id:
            return r
    return None


def create_entry(payload: dict) -> dict:
    """Write a new OPEN position from a discovery entry_payload."""
    now = datetime.now(timezone.utc).isoformat()
    fill_price = min(
        payload["no_best_ask_at_entry"] + config.ENTRY_SLIPPAGE,
        config.MAX_FILL_PRICE,
    )
    shares = config.SIMULATED_POSITION_USD / fill_price

    # Build correct Polymarket URL: /event/<event_slug> (NOT market slug).
    # Falls back to market slug if eventSlug missing — at worst the page 404s
    # but at least the dashboard table doesn't crash.
    event_slug = payload.get("event_slug") or payload.get("slug") or ""
    polymarket_url = f"https://polymarket.com/event/{event_slug}" if event_slug else ""

    row = {
        "entry_ts": now,
        "market_id": payload["market_id"],
        "slug": payload["slug"],
        "event_slug": payload.get("event_slug", ""),
        "condition_id": payload.get("condition_id", ""),
        "question": payload["question"],
        "cluster_auto": payload["cluster_auto"],
        "cluster_override": "",
        "ttr_at_entry_hours": payload["ttr_at_entry_hours"],
        "no_token_id": payload["no_token_id"],
        "yes_token_id": payload["yes_token_id"],
        "no_best_ask_at_entry": payload["no_best_ask_at_entry"],
        "no_best_bid_at_entry": payload["no_best_bid_at_entry"],
        "simulated_fill_price": round(fill_price, 4),
        "simulated_shares": round(shares, 6),
        "volume_at_entry": payload["volume_at_entry"],
        "book_depth_no_top3": payload["book_depth_no_top3"],
        "uma_risk_flag": payload["uma_risk_flag"],
        "end_date_iso": payload["end_date_iso"],
        "status": "OPEN",
        "exit_ts": "", "exit_price": "", "exit_reason": "", "pnl": "",
        "current_no_ask": payload["no_best_ask_at_entry"],
        "current_no_bid": payload["no_best_bid_at_entry"],
        "last_price_ts": now,
        "verify_first_poll_ts": "",
        "verify_first_poll_winner": "",
        "verify_last_attempt_ts": "",
        "verify_attempt_count": 0,
        "polymarket_url": polymarket_url,
    }
    _assert_no_nan(row, REQUIRED_ON_ENTRY)
    _write_row(config.POSITIONS_CSV, POSITION_COLS, row)
    log.info("ENTRY %s | %s | NO@%.3f | cluster=%s | TTR=%.1fh",
             payload["market_id"][:10], payload["slug"][:50],
             fill_price, payload["cluster_auto"], payload["ttr_at_entry_hours"])
    return row


def update_open_price(market_id: str, no_ask: float, no_bid: float, ttr_hours: float):
    """Update current price fields on an open position. Append to pricelog."""
    rows = _read_all(config.POSITIONS_CSV, POSITION_COLS)
    found = False
    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        if r.get("market_id") == market_id and r.get("status") == "OPEN":
            r["current_no_ask"] = round(no_ask, 4) if no_ask is not None else ""
            r["current_no_bid"] = round(no_bid, 4) if no_bid is not None else ""
            r["last_price_ts"] = now
            found = True
            break
    if found:
        _rewrite_all(config.POSITIONS_CSV, POSITION_COLS, rows)
        _write_row(config.PRICELOG_CSV, PRICELOG_COLS, {
            "ts": now,
            "market_id": market_id,
            "no_best_ask": round(no_ask, 4) if no_ask is not None else "",
            "no_best_bid": round(no_bid, 4) if no_bid is not None else "",
            "ttr_remaining_hours": round(ttr_hours, 2) if ttr_hours is not None else "",
        })


def manual_sell(market_id: str) -> Optional[dict]:
    """Atomic manual-sell. Returns the updated row, or None if not found/already closed."""
    rows = _read_all(config.POSITIONS_CSV, POSITION_COLS)
    target = None
    for r in rows:
        if r.get("market_id") == market_id and r.get("status") == "OPEN":
            target = r
            break
    if not target:
        return None
    now = datetime.now(timezone.utc).isoformat()
    try:
        bid = float(target.get("current_no_bid") or target.get("no_best_bid_at_entry") or 0)
    except (TypeError, ValueError):
        bid = 0.0
    exit_price = max(0.0, bid - config.EXIT_SLIPPAGE)
    fill = float(target["simulated_fill_price"])
    shares = float(target["simulated_shares"])
    pnl = round((exit_price - fill) * shares, 6)

    target["status"] = "MANUAL_SOLD"
    target["exit_ts"] = now
    target["exit_price"] = round(exit_price, 4)
    target["exit_reason"] = "MANUAL"
    target["pnl"] = pnl
    _rewrite_all(config.POSITIONS_CSV, POSITION_COLS, rows)
    log.info("MANUAL_SELL %s | exit=%.3f | pnl=%+.4f", market_id[:10], exit_price, pnl)
    return target


def reclassify(market_id: str, new_cluster: str) -> bool:
    """Set cluster_override on a position. Preserves cluster_auto for accuracy stats."""
    rows = _read_all(config.POSITIONS_CSV, POSITION_COLS)
    found = False
    for r in rows:
        if r.get("market_id") == market_id:
            r["cluster_override"] = new_cluster
            found = True
            break
    if found:
        _rewrite_all(config.POSITIONS_CSV, POSITION_COLS, rows)
    return found


RESOLVED_STATUSES = {
    "RESOLVED_NO", "RESOLVED_YES",
    "MANUAL_RESOLVED_NO", "MANUAL_RESOLVED_YES",
    "MANUAL_SOLD",
}


def is_resolved(status: str) -> bool:
    """Once resolved, immutable. resolution_loop must skip these."""
    return status in RESOLVED_STATUSES


def set_status(market_id: str, new_status: str, allow_overwrite_resolved: bool = False) -> bool:
    """Set position status. Refuses to overwrite a resolved status by default."""
    rows = _read_all(config.POSITIONS_CSV, POSITION_COLS)
    found = False
    for r in rows:
        if r.get("market_id") == market_id:
            if is_resolved(r.get("status", "")) and not allow_overwrite_resolved:
                log.warning("Refusing to overwrite resolved status %s on %s",
                            r.get("status"), market_id[:10])
                return False
            r["status"] = new_status
            found = True
            break
    if found:
        _rewrite_all(config.POSITIONS_CSV, POSITION_COLS, rows)
    return found


def log_resolution_attempt(row: dict):
    """Append a row to the resolutions audit CSV."""
    _write_row(config.RESOLUTIONS_CSV, RESOLUTION_COLS, row)


def resolve_position(market_id: str, no_won: bool,
                     manual: bool = False) -> Optional[dict]:
    """Mark position as resolved. Refuses if already resolved (immutability).

    `manual=True` records as MANUAL_RESOLVED_* so audit trail distinguishes
    auto-verified from human-confirmed.
    """
    rows = _read_all(config.POSITIONS_CSV, POSITION_COLS)
    target = None
    for r in rows:
        if r.get("market_id") == market_id:
            target = r
            break
    if not target:
        return None
    if is_resolved(target.get("status", "")):
        log.warning("Refusing to re-resolve %s (already %s)",
                    market_id[:10], target.get("status"))
        return None

    now = datetime.now(timezone.utc).isoformat()
    fill = float(target["simulated_fill_price"])
    shares = float(target["simulated_shares"])
    if no_won:
        exit_price = 1.0
        status = "MANUAL_RESOLVED_NO" if manual else "RESOLVED_NO"
        reason = "MANUAL_NO_WIN" if manual else "RESOLVED_NO_WIN"
    else:
        exit_price = 0.0
        status = "MANUAL_RESOLVED_YES" if manual else "RESOLVED_YES"
        reason = "MANUAL_YES_LOSS" if manual else "RESOLVED_YES_LOSS"
    pnl = round((exit_price - fill) * shares, 6)

    target["status"] = status
    target["exit_ts"] = now
    target["exit_price"] = exit_price
    target["exit_reason"] = reason
    target["pnl"] = pnl
    _rewrite_all(config.POSITIONS_CSV, POSITION_COLS, rows)
    log.info("RESOLVE %s | %s | pnl=%+.4f", market_id[:10], status, pnl)
    return target


def record_first_poll(market_id: str, winner: str) -> bool:
    """Persist poll #1 winner so the 2nd-poll comparison survives restarts.

    `winner` is one of "yes", "no", or "unclear".
    """
    rows = _read_all(config.POSITIONS_CSV, POSITION_COLS)
    found = False
    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        if r.get("market_id") == market_id:
            if is_resolved(r.get("status", "")):
                return False
            r["verify_first_poll_ts"] = now
            r["verify_first_poll_winner"] = winner
            r["verify_last_attempt_ts"] = now
            try:
                r["verify_attempt_count"] = int(r.get("verify_attempt_count") or 0) + 1
            except (TypeError, ValueError):
                r["verify_attempt_count"] = 1
            r["status"] = "RESOLUTION_AWAITING_2ND"
            found = True
            break
    if found:
        _rewrite_all(config.POSITIONS_CSV, POSITION_COLS, rows)
    return found


def record_verify_attempt(market_id: str, new_status: Optional[str] = None) -> bool:
    """Bump verify_attempt_count + verify_last_attempt_ts. Optionally set status."""
    rows = _read_all(config.POSITIONS_CSV, POSITION_COLS)
    found = False
    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        if r.get("market_id") == market_id:
            if is_resolved(r.get("status", "")):
                return False
            r["verify_last_attempt_ts"] = now
            try:
                r["verify_attempt_count"] = int(r.get("verify_attempt_count") or 0) + 1
            except (TypeError, ValueError):
                r["verify_attempt_count"] = 1
            if new_status:
                r["status"] = new_status
            found = True
            break
    if found:
        _rewrite_all(config.POSITIONS_CSV, POSITION_COLS, rows)
    return found


def positions_needing_verification() -> list:
    """All positions in any pre-resolved verification state (excludes immutable resolved)."""
    rows = _read_all(config.POSITIONS_CSV, POSITION_COLS)
    pending_states = {"OPEN", "RESOLUTION_PENDING_VERIFY",
                      "RESOLUTION_AWAITING_2ND", "UMA_PENDING",
                      "RESOLUTION_DISPUTED"}
    return [r for r in rows if r.get("status") in pending_states]


def mark_uma_pending(market_id: str):
    rows = _read_all(config.POSITIONS_CSV, POSITION_COLS)
    for r in rows:
        if r.get("market_id") == market_id and not is_resolved(r.get("status", "")):
            r["status"] = "UMA_PENDING"
            _rewrite_all(config.POSITIONS_CSV, POSITION_COLS, rows)
            return


def log_discovery(row: dict):
    _write_row(config.DISCOVERY_CSV, DISCOVERY_COLS, row)


def write_heartbeat():
    os.makedirs(config.STATE_DIR, exist_ok=True)
    with open(config.HEARTBEAT_FILE, "w") as f:
        f.write(datetime.now(timezone.utc).isoformat())


# ---------- Stats for dashboard ----------

def compute_summary(scaled: bool = False) -> dict:
    rows = all_positions()
    mult = config.SCALED_VIEW_MULTIPLIER if scaled else 1.0
    total = len(rows)
    open_n = sum(1 for r in rows if r.get("status") == "OPEN")
    win_n = sum(1 for r in rows if r.get("status") in ("RESOLVED_NO", "MANUAL_RESOLVED_NO"))
    loss_n = sum(1 for r in rows if r.get("status") in ("RESOLVED_YES", "MANUAL_RESOLVED_YES"))
    sold_n = sum(1 for r in rows if r.get("status") == "MANUAL_SOLD")
    pending_n = sum(1 for r in rows if r.get("status") == "UMA_PENDING")
    verify_n = sum(1 for r in rows if r.get("status") in
                   ("RESOLUTION_PENDING_VERIFY", "RESOLUTION_AWAITING_2ND"))
    disputed_n = sum(1 for r in rows if r.get("status") == "RESOLUTION_DISPUTED")
    pnl = 0.0
    for r in rows:
        try:
            if r.get("pnl") not in (None, ""):
                pnl += float(r["pnl"])
        except (TypeError, ValueError):
            pass
    closed = win_n + loss_n + sold_n
    win_rate = (win_n / closed * 100.0) if closed > 0 else None
    return {
        "total": total,
        "open": open_n,
        "win": win_n,
        "loss": loss_n,
        "sold": sold_n,
        "uma_pending": pending_n,
        "verify_pending": verify_n,
        "disputed": disputed_n,
        "pnl_usd": round(pnl * mult, 4),
        "win_rate_pct": round(win_rate, 1) if win_rate is not None else None,
        "scaled": scaled,
    }
