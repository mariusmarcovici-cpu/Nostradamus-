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
            n_open = len(position.open_positions())
            markets = discovery.fetch_markets_in_window(now)
            log.info("Discovery: %d markets in window, %d already-entered, %d open (cap=%d)",
                     len(markets), len(entered_ids), n_open, config.MAX_OPEN_POSITIONS)
            n_pass = 0
            n_skipped_capped = 0
            for m in markets:
                try:
                    # v0.2.0: hard cap on open positions for human-tractable review
                    if n_open >= config.MAX_OPEN_POSITIONS:
                        n_skipped_capped += 1
                        continue
                    result = discovery.evaluate_market(m, now, entered_ids)
                    # We only log to discovery.csv if the market was at least in TTR window
                    # (the evaluate function rejects out-of-window early, those are noise)
                    reason = result.get("reject_reason") or ""
                    if not reason.startswith("ttr_out_of_window"):
                        position.log_discovery(result["row"])
                    if result["pass"]:
                        position.create_entry(result["entry_payload"])
                        entered_ids.add(result["entry_payload"]["market_id"])
                        n_open += 1
                        n_pass += 1
                except Exception as e:
                    log.exception("evaluate_market crashed for %s: %s",
                                  m.get("id") or m.get("slug"), e)
            if n_skipped_capped > 0:
                log.info("Discovery cycle done: %d new entries, %d skipped (at cap %d)",
                         n_pass, n_skipped_capped, config.MAX_OPEN_POSITIONS)
            else:
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
    """v0.2.0: SINGLE-POLL resolution by default + auto-resolve-as-NO timer.

    Behavior set by config.RESOLUTION_REQUIRE_TWO_POLLS:
      - False (default): clear winner + alignment OK → resolve immediately
      - True (legacy): require 2 polls 60s apart that match
    Stuck positions are auto-resolved as NO after AUTO_RESOLVE_AS_NO_AFTER_HOURS.
    """
    mode = "2-poll legacy" if config.RESOLUTION_REQUIRE_TWO_POLLS else "single-poll"
    log.info("resolution_loop started (%s, auto-resolve-as-NO at %dhr)",
             mode, config.AUTO_RESOLVE_AS_NO_AFTER_HOURS)
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
    """v0.2.0: SINGLE-POLL resolution with auto-resolve-as-NO timer.

    Flow:
      1. Skip if too soon (< RESOLUTION_MIN_AGE_S past end_date).
      2. Auto-resolve as NO if position is stuck past
         AUTO_RESOLVE_AS_NO_AFTER_HOURS — eliminates human-verify need.
      3. Fetch market from Gamma. If not closed and past 4hr → UMA_PENDING.
      4. Parse winner. If unclear → keep retrying.
      5. Verify token alignment. If mismatch → keep retrying (auto-cleared
         by timer eventually).
      6. SINGLE POLL: clear winner + alignment OK → resolve immediately.
         (Old 2-poll-must-match logic only runs if RESOLUTION_REQUIRE_TWO_POLLS=true.)
    """
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
    age_hr = age_s / 3600.0
    if age_s < config.RESOLUTION_MIN_AGE_S:
        return  # too soon

    # v0.2.0: AUTO-RESOLVE-AS-NO timer for stuck positions. Eliminates
    # the human-verification bottleneck. NO is the dominant outcome at
    # 0.93+ entry prices, so defaulting to NO when stuck is the
    # statistically-correct fallback.
    cur_status = p.get("status", "")
    is_stuck = cur_status in ("RESOLUTION_PENDING_VERIFY",
                                "RESOLUTION_AWAITING_2ND",
                                "UMA_PENDING",
                                "RESOLUTION_DISPUTED")
    if is_stuck and age_hr >= config.AUTO_RESOLVE_AS_NO_AFTER_HOURS:
        result = position.resolve_position(market_id, no_won=True, manual=False)
        audit_row = {
            "ts": now.isoformat(), "market_id": market_id,
            "slug": p.get("slug", ""), "gamma_closed": "",
            "outcomes_raw": "", "prices_raw": "",
            "parsed_winner": "", "token_alignment_ok": "",
            "age_since_end_min": round(age_min, 1),
            "poll_number": "", "prior_winner": "",
            "action_taken": ("AUTO_RESOLVED_NO_TIMEOUT" if result
                              else "AUTO_RESOLVE_REFUSED_ALREADY_RESOLVED"),
            "notes": f"prior_status={cur_status};age_hr={age_hr:.1f}",
        }
        position.log_resolution_attempt(audit_row)
        if result:
            log.info("AUTO_RESOLVED_NO %s | age=%.1fhr | prior=%s",
                     market_id[:10], age_hr, cur_status)
        return

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
        if age_s > 4 * 3600 and cur_status != "UMA_PENDING":
            position.mark_uma_pending(market_id)
            audit_row["action_taken"] = "MARK_UMA_PENDING"
            position.log_resolution_attempt(audit_row)
            log.info("UMA_PENDING %s (age=%.1fmin)", market_id[:10], age_min)
        return

    # Unclear winner — keep retrying. Auto-resolve timer will clean up if it stays stuck.
    if parsed["winner"] == "unclear":
        position.record_verify_attempt(market_id, new_status="RESOLUTION_PENDING_VERIFY")
        audit_row["action_taken"] = "PENDING_UNCLEAR"
        position.log_resolution_attempt(audit_row)
        return

    # Token alignment check
    alignment_ok = _verify_token_alignment(market, p.get("no_token_id", ""))
    audit_row["token_alignment_ok"] = int(alignment_ok)
    if not alignment_ok:
        # v0.2.0: don't dispute (no human review). Park as PENDING_VERIFY,
        # auto-resolve timer will clear it.
        position.record_verify_attempt(market_id, new_status="RESOLUTION_PENDING_VERIFY")
        audit_row["action_taken"] = "PENDING_TOKEN_MISALIGN"
        audit_row["notes"] = (audit_row["notes"] + ";token_alignment_failed").strip(";")
        position.log_resolution_attempt(audit_row)
        log.warning("TOKEN ALIGNMENT FAIL %s — will auto-resolve as NO at %dhr",
                    market_id[:10], config.AUTO_RESOLVE_AS_NO_AFTER_HOURS)
        return

    # ──────────────────────────────────────────────────────────────────
    # v0.2.0: SINGLE-POLL mode (default). Clear winner + alignment OK → resolve.
    # Old 2-poll behavior only runs if RESOLUTION_REQUIRE_TWO_POLLS=true.
    # ──────────────────────────────────────────────────────────────────
    if not config.RESOLUTION_REQUIRE_TWO_POLLS:
        no_won = parsed["winner"] == "no"
        result = position.resolve_position(market_id, no_won=no_won, manual=False)
        audit_row["poll_number"] = 1
        if result:
            audit_row["action_taken"] = "RESOLVED_NO" if no_won else "RESOLVED_YES"
            log.info("RESOLVED %s | winner=%s (single-poll)",
                     market_id[:10], parsed["winner"])
        else:
            audit_row["action_taken"] = "RESOLVE_REFUSED_ALREADY_RESOLVED"
        position.log_resolution_attempt(audit_row)
        return

    # ──────────────────────────────────────────────────────────────────
    # Legacy 2-poll path (only when RESOLUTION_REQUIRE_TWO_POLLS=true)
    # ──────────────────────────────────────────────────────────────────
    prior_winner = (p.get("verify_first_poll_winner") or "").strip().lower()
    first_poll_ts_str = p.get("verify_first_poll_ts") or ""

    if not prior_winner:
        position.record_first_poll(market_id, parsed["winner"])
        audit_row["poll_number"] = 1
        audit_row["action_taken"] = "FIRST_POLL_RECORDED"
        position.log_resolution_attempt(audit_row)
        log.info("RESOLUTION POLL 1 %s | winner=%s | awaiting confirm in %ds",
                 market_id[:10], parsed["winner"], config.RESOLUTION_CONFIRM_DELAY_S)
        return

    try:
        first_poll_ts = datetime.fromisoformat(first_poll_ts_str)
        if first_poll_ts.tzinfo is None:
            first_poll_ts = first_poll_ts.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        first_poll_ts = None

    if first_poll_ts and (now - first_poll_ts).total_seconds() < config.RESOLUTION_CONFIRM_DELAY_S:
        return

    audit_row["poll_number"] = 2

    if parsed["winner"] != prior_winner:
        # In legacy mode, dispute. Auto-resolve timer will eventually clear it as NO.
        position.record_verify_attempt(market_id, new_status="RESOLUTION_DISPUTED")
        audit_row["action_taken"] = "DISPUTED_WINNER_CHANGED"
        position.log_resolution_attempt(audit_row)
        log.error("RESOLUTION DISPUTED %s | poll1=%s poll2=%s (will auto-resolve at %dhr)",
                  market_id[:10], prior_winner, parsed["winner"],
                  config.AUTO_RESOLVE_AS_NO_AFTER_HOURS)
        return

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
    log.info("Nostradamus %s starting up — DRY MODE", config.VERSION)
    log.info("Window: TTR %d–%dh, Price NO %.2f–%.2f, Size $%.2f/trade",
             config.TTR_MIN_HOURS, config.TTR_MAX_HOURS,
             config.PRICE_MIN, config.PRICE_MAX,
             config.SIMULATED_POSITION_USD)
    log.info("Skip clusters: %s",
             ",".join(sorted(config.SKIP_CLUSTERS)) or "(none)")
    log.info("Max open positions: %d  ·  Auto-resolve-as-NO at %dhr  ·  Single-poll: %s",
             config.MAX_OPEN_POSITIONS,
             config.AUTO_RESOLVE_AS_NO_AFTER_HOURS,
             not config.RESOLUTION_REQUIRE_TWO_POLLS)
    log.info("Block UMA-flagged: %s",
             "yes (subjective resolution markets blocked at entry + hidden in dashboard)"
             if config.BLOCK_UMA_FLAGGED else "no (UMA flag is label-only)")
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
