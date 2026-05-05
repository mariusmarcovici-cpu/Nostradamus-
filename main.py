"""SentiBot main entry point.

Runs three background threads + Flask dashboard in main thread:
  - discovery_loop: poll Gamma every DISCOVERY_INTERVAL_S, evaluate, enter
  - position_loop: refresh open positions, log prices, drop to 5s in final hour
  - resolution_loop: check past-end_date markets, finalize PnL
  - heartbeat is bumped from each loop iteration

Dashboard runs on $PORT (Railway) or DASHBOARD_PORT_DEFAULT.
"""

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone

import config
import dashboard
import discovery
import position

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("sentibot")


# ---------- Loops ----------

def discovery_loop():
    log.info("discovery_loop started")
    while True:
        try:
            now = datetime.now(timezone.utc)
            entered_ids = position.all_entered_market_ids()
            markets = discovery.fetch_markets_in_window(now)
            log.info("Discovery: %d markets in window, %d already-entered",
                     len(markets), len(entered_ids))
            n_pass = 0
            for m in markets:
                try:
                    result = discovery.evaluate_market(m, now, entered_ids)
                    # We only log to discovery.csv if the market was at least in TTR window
                    # (the evaluate function rejects out-of-window early, those are noise)
                    reason = result.get("reject_reason") or ""
                    if not reason.startswith("ttr_out_of_window"):
                        position.log_discovery(result["row"])
                    if result["pass"]:
                        position.create_entry(result["entry_payload"])
                        entered_ids.add(result["entry_payload"]["market_id"])
                        n_pass += 1
                except Exception as e:
                    log.exception("evaluate_market crashed for %s: %s",
                                  m.get("id") or m.get("slug"), e)
            log.info("Discovery cycle done: %d new entries", n_pass)
            position.write_heartbeat()
        except Exception as e:
            log.exception("discovery_loop iteration crashed: %s", e)
        time.sleep(config.DISCOVERY_INTERVAL_S)


def position_loop():
    """Refresh prices on all OPEN positions. Cadence drops to 5s in final hour."""
    log.info("position_loop started")
    while True:
        try:
            positions = position.open_positions()
            now = datetime.now(timezone.utc)
            in_final_hour = False
            for p in positions:
                try:
                    end_iso = p.get("end_date_iso") or ""
                    end_dt = None
                    ttr_remaining = None
                    if end_iso:
                        end_dt = datetime.fromisoformat(end_iso)
                        if end_dt.tzinfo is None:
                            end_dt = end_dt.replace(tzinfo=timezone.utc)
                        ttr_remaining = (end_dt - now).total_seconds() / 3600.0
                        if (end_dt - now).total_seconds() < config.FINAL_HOUR_THRESHOLD_S:
                            in_final_hour = True
                    book = discovery.fetch_orderbook(p["no_token_id"])
                    ask, bid = discovery.best_ask_and_bid(book)
                    position.update_open_price(p["market_id"], ask, bid, ttr_remaining or 0)
                except Exception as e:
                    log.warning("position_loop price-update failed for %s: %s",
                                p.get("market_id"), e)
            position.write_heartbeat()
            sleep_s = config.FINAL_HOUR_INTERVAL_S if in_final_hour else config.PRICE_UPDATE_INTERVAL_S
        except Exception as e:
            log.exception("position_loop crashed: %s", e)
            sleep_s = config.PRICE_UPDATE_INTERVAL_S
        time.sleep(sleep_s)


def _parse_resolution_from_market(market: dict) -> dict:
    """Parse a Gamma market dict and return verification result.

    Returns:
        {
            "winner": "yes" | "no" | "unclear",
            "outcomes_raw": str,
            "prices_raw": str,
            "token_alignment_ok": bool,
            "gamma_closed": bool,
            "notes": str,
        }
    """
    out = {
        "winner": "unclear",
        "outcomes_raw": "",
        "prices_raw": "",
        "token_alignment_ok": False,
        "gamma_closed": bool(market.get("closed") or market.get("resolved")),
        "notes": "",
    }
    outcomes_raw = market.get("outcomes")
    prices_raw = market.get("outcomePrices")
    out["outcomes_raw"] = outcomes_raw if isinstance(outcomes_raw, str) else json.dumps(outcomes_raw or [])
    out["prices_raw"] = prices_raw if isinstance(prices_raw, str) else json.dumps(prices_raw or [])

    if not outcomes_raw or not prices_raw:
        out["notes"] = "missing_outcomes_or_prices"
        return out
    try:
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
    except (ValueError, TypeError) as e:
        out["notes"] = f"bad_json:{e}"
        return out

    if len(outcomes) != 2 or len(prices) != 2:
        out["notes"] = f"non_binary_or_malformed_len_{len(outcomes)}_{len(prices)}"
        return out

    try:
        prices_f = [float(p) for p in prices]
    except (ValueError, TypeError):
        out["notes"] = "non_numeric_prices"
        return out

    # Sum-to-1 sanity check
    if abs(sum(prices_f) - 1.0) > config.RESOLUTION_PRICE_SUM_TOLERANCE:
        out["notes"] = f"prices_dont_sum_to_1:{sum(prices_f):.4f}"
        return out

    # Find clear winner
    winner_idx = None
    for i, p in enumerate(prices_f):
        if p >= config.RESOLUTION_WINNER_THRESHOLD:
            winner_idx = i
            break
    if winner_idx is None:
        out["notes"] = f"no_clear_winner:max={max(prices_f):.4f}"
        return out

    winner_label = (outcomes[winner_idx] or "").strip().lower()
    if winner_label not in ("yes", "no"):
        out["notes"] = f"unexpected_outcome_label:{winner_label}"
        return out
    out["winner"] = winner_label
    out["token_alignment_ok"] = True  # parse succeeded; per-position alignment checked below
    return out


def _verify_token_alignment(market: dict, stored_no_token_id: str) -> bool:
    """Re-parse Gamma's outcomes/tokens and confirm stored no_token_id still
    aligns to the 'No' string. Defends against the v5.5.x direction-bug pattern
    in resolution form.
    """
    parsed = discovery.parse_outcomes_and_tokens(market)
    if not parsed:
        return False
    return parsed.get("no_token_id") == str(stored_no_token_id)


def resolution_loop():
    """Strict 2-poll resolution verification.

    Flow:
      1. Skip resolved positions (immutable).
      2. Skip if not yet past end_date + RESOLUTION_MIN_AGE_S.
      3. Fetch market from Gamma. If not closed yet:
           - if past end + 4hrs: mark UMA_PENDING.
           - else: leave as OPEN, retry next loop.
      4. Parse outcomes/prices. Verify token alignment.
      5. If poll #1: record winner, set RESOLUTION_AWAITING_2ND, log audit row.
      6. If poll #2 (≥ RESOLUTION_CONFIRM_DELAY_S after poll #1):
           - winners match → resolve (immutable), log audit row.
           - winners differ → RESOLUTION_DISPUTED, log audit row.
    """
    log.info("resolution_loop started (strict 2-poll mode)")
    while True:
        try:
            now = datetime.now(timezone.utc)
            for p in position.positions_needing_verification():
                try:
                    _process_one_resolution(p, now)
                except Exception as e:
                    log.warning("resolution processing failed for %s: %s",
                                p.get("market_id"), e)
        except Exception as e:
            log.exception("resolution_loop iteration crashed: %s", e)
        time.sleep(config.RESOLUTION_CHECK_INTERVAL_S)


def _process_one_resolution(p: dict, now: datetime):
    market_id = p["market_id"]
    end_iso = p.get("end_date_iso") or ""
    if not end_iso:
        return
    try:
        end_dt = datetime.fromisoformat(end_iso)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return

    age_s = (now - end_dt).total_seconds()
    age_min = age_s / 60.0
    if age_s < config.RESOLUTION_MIN_AGE_S:
        return  # too soon

    market = discovery.fetch_market_by_id(market_id)
    if not market:
        return

    parsed = _parse_resolution_from_market(market)
    audit_row = {
        "ts": now.isoformat(),
        "market_id": market_id,
        "slug": p.get("slug", ""),
        "gamma_closed": int(parsed["gamma_closed"]),
        "outcomes_raw": parsed["outcomes_raw"],
        "prices_raw": parsed["prices_raw"],
        "parsed_winner": parsed["winner"],
        "token_alignment_ok": "",
        "age_since_end_min": round(age_min, 1),
        "poll_number": "",
        "prior_winner": p.get("verify_first_poll_winner") or "",
        "action_taken": "",
        "notes": parsed["notes"],
    }

    # If Gamma still doesn't say closed, escalate to UMA_PENDING after 4hrs
    if not parsed["gamma_closed"]:
        if age_s > 4 * 3600 and p.get("status") != "UMA_PENDING":
            position.mark_uma_pending(market_id)
            audit_row["action_taken"] = "MARK_UMA_PENDING"
            position.log_resolution_attempt(audit_row)
            log.info("UMA_PENDING %s (age=%.1fmin)", market_id[:10], age_min)
        return

    # If parse couldn't determine a clear winner, stay in pending and retry
    if parsed["winner"] == "unclear":
        position.record_verify_attempt(market_id, new_status="RESOLUTION_PENDING_VERIFY")
        audit_row["action_taken"] = "PENDING_UNCLEAR"
        position.log_resolution_attempt(audit_row)
        return

    # Token alignment check — defends against direction-bug pattern
    alignment_ok = _verify_token_alignment(market, p.get("no_token_id", ""))
    audit_row["token_alignment_ok"] = int(alignment_ok)
    if not alignment_ok:
        position.record_verify_attempt(market_id, new_status="RESOLUTION_DISPUTED")
        audit_row["action_taken"] = "DISPUTED_TOKEN_MISALIGN"
        audit_row["notes"] = (audit_row["notes"] + ";token_alignment_failed").strip(";")
        position.log_resolution_attempt(audit_row)
        log.error("TOKEN ALIGNMENT FAIL %s — manual review required", market_id[:10])
        return

    # Two-poll confirmation logic
    prior_winner = (p.get("verify_first_poll_winner") or "").strip().lower()
    first_poll_ts_str = p.get("verify_first_poll_ts") or ""

    if not prior_winner:
        # This is poll #1
        position.record_first_poll(market_id, parsed["winner"])
        audit_row["poll_number"] = 1
        audit_row["action_taken"] = "FIRST_POLL_RECORDED"
        position.log_resolution_attempt(audit_row)
        log.info("RESOLUTION POLL 1 %s | winner=%s | awaiting confirm in %ds",
                 market_id[:10], parsed["winner"], config.RESOLUTION_CONFIRM_DELAY_S)
        return

    # Poll #2+ — confirm prior winner and that enough time has passed
    try:
        first_poll_ts = datetime.fromisoformat(first_poll_ts_str)
        if first_poll_ts.tzinfo is None:
            first_poll_ts = first_poll_ts.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        first_poll_ts = None

    if first_poll_ts and (now - first_poll_ts).total_seconds() < config.RESOLUTION_CONFIRM_DELAY_S:
        # Not enough time has passed since poll #1 — skip this iteration
        return

    audit_row["poll_number"] = 2

    if parsed["winner"] != prior_winner:
        # Winners disagreed between polls — never auto-resolve
        position.record_verify_attempt(market_id, new_status="RESOLUTION_DISPUTED")
        audit_row["action_taken"] = "DISPUTED_WINNER_CHANGED"
        position.log_resolution_attempt(audit_row)
        log.error("RESOLUTION DISPUTED %s | poll1=%s poll2=%s — manual review required",
                  market_id[:10], prior_winner, parsed["winner"])
        return

    # Both polls agree — resolve immutably
    no_won = parsed["winner"] == "no"
    result = position.resolve_position(market_id, no_won=no_won, manual=False)
    if result:
        audit_row["action_taken"] = "RESOLVED_NO" if no_won else "RESOLVED_YES"
    else:
        audit_row["action_taken"] = "RESOLVE_REFUSED_ALREADY_RESOLVED"
    position.log_resolution_attempt(audit_row)


# ---------- Entrypoint ----------

def main():
    log.info("=" * 60)
    log.info("SentiBot %s starting up — DRY MODE", config.VERSION)
    log.info("Window: TTR %d–%dh, Price NO %.2f–%.2f, Size $%.2f/trade",
             config.TTR_MIN_HOURS, config.TTR_MAX_HOURS,
             config.PRICE_MIN, config.PRICE_MAX,
             config.SIMULATED_POSITION_USD)
    log.info("=" * 60)

    position.init_state()
    position.write_heartbeat()

    threads = [
        threading.Thread(target=discovery_loop, name="discovery", daemon=True),
        threading.Thread(target=position_loop, name="position", daemon=True),
        threading.Thread(target=resolution_loop, name="resolution", daemon=True),
    ]
    for t in threads:
        t.start()
        log.info("Started thread: %s", t.name)

    port = int(os.environ.get("PORT", config.DASHBOARD_PORT_DEFAULT))
    log.info("Starting dashboard on port %d", port)
    dashboard.run(port)


if __name__ == "__main__":
    main()
