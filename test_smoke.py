"""Smoke test for SentiBot v1. Exercises filter chain, CSV lifecycle, manual sell, reclassify.

Does NOT hit the network. Mocks Gamma/CLOB responses.
"""

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

# Use a temp state dir so we don't pollute the real one
TMP = tempfile.mkdtemp(prefix="sentibot_test_")
sys.path.insert(0, "/home/claude/sentibot")
import config
config.STATE_DIR = TMP
config.POSITIONS_CSV = os.path.join(TMP, "p.csv")
config.DISCOVERY_CSV = os.path.join(TMP, "d.csv")
config.PRICELOG_CSV = os.path.join(TMP, "l.csv")
config.HEARTBEAT_FILE = os.path.join(TMP, "hb.txt")

import position
import discovery
import cluster
import blacklists


def test_blacklist():
    assert blacklists.is_crypto("will-btc-hit-100k", [], "Will BTC hit $100k?")
    assert blacklists.is_crypto("foo", [{"label": "Bitcoin"}], "Foo")
    assert blacklists.is_crypto("foo", [], "Will ETH go up?")
    assert not blacklists.is_crypto("lakers-vs-thunder", [{"label": "NBA"}], "Will the Lakers win?")
    assert not blacklists.is_crypto("trump-tariff", [{"label": "Politics"}], "Will Trump impose tariffs?")
    assert blacklists.has_uma_dispute_marker("Resolved by consensus of credible reporting")
    assert not blacklists.has_uma_dispute_marker("Resolved by Numbers.com")
    print("✅ test_blacklist")


def test_cluster():
    assert cluster.classify("Will Lakers win Game 1?", [{"label": "NBA"}]) == "A_SPORTS"
    assert cluster.classify("Will Trump say Peacemaker this week?", []) == "B_MENTION"
    assert cluster.classify("Will NYC high temperature exceed 70F on May 6?", [{"label": "Weather"}]) == "C_WEATHER"
    assert cluster.classify("Devil Wears Prada 2 opening weekend box office", []) == "D_BOXOFFICE"
    assert cluster.classify("Will there be a ceasefire between Israel and Hamas?", [{"label": "Geopolitics"}]) == "E_GEOPOLITICS"
    assert cluster.classify("Will Trump fire Powell?", [{"label": "Politics"}]) == "F_POLITICS"
    assert cluster.classify("Apple Q2 earnings beat?", [{"label": "Earnings"}]) == "G_TECH_CORP"
    assert cluster.classify("Random thing", []) == "H_OTHER"
    print("✅ test_cluster")


def test_outcome_parsing():
    market = {
        "id": "m1",
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps(["yes_tok", "no_tok"]),
    }
    tokens = discovery.parse_outcomes_and_tokens(market)
    assert tokens == {"yes_token_id": "yes_tok", "no_token_id": "no_tok"}, tokens

    # Reverse order — must use index alignment, not field order
    market2 = {
        "id": "m2",
        "outcomes": json.dumps(["No", "Yes"]),
        "clobTokenIds": json.dumps(["no_tok2", "yes_tok2"]),
    }
    tokens2 = discovery.parse_outcomes_and_tokens(market2)
    assert tokens2 == {"yes_token_id": "yes_tok2", "no_token_id": "no_tok2"}, tokens2

    # Bad market
    assert discovery.parse_outcomes_and_tokens({"id": "m3"}) is None
    print("✅ test_outcome_parsing")


def test_book_parsing():
    book = {
        "asks": [{"price": "0.93", "size": "100"}, {"price": "0.94", "size": "50"}],
        "bids": [{"price": "0.91", "size": "200"}, {"price": "0.90", "size": "100"}],
    }
    ask, bid = discovery.best_ask_and_bid(book)
    assert ask == 0.93 and bid == 0.91, (ask, bid)
    depth = discovery.book_depth_top3(book, "asks")
    assert depth == 150.0, depth
    print("✅ test_book_parsing")


def test_filter_chain_pass():
    """A realistic NBA market that should pass all filters."""
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=36)
    market = {
        "id": "m_nba_1",
        "slug": "lakers-vs-thunder-game-1",
        "question": "Will the Lakers win Game 1 vs Thunder?",
        "tags": [{"label": "NBA"}],
        "category": "Sports",
        "endDate": end.isoformat(),
        "closed": False,
        "active": True,
        "enableOrderBook": True,
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps(["yes_t", "no_t"]),
        "volume": 606000,
        "resolutionSource": "Resolved by official NBA box score.",
        "description": "Game 1 of NBA Playoffs first round.",
    }
    fake_book = {
        "asks": [{"price": "0.90", "size": "1000"}, {"price": "0.91", "size": "500"}],
        "bids": [{"price": "0.88", "size": "800"}],
    }
    with patch.object(discovery, "fetch_orderbook", return_value=fake_book):
        result = discovery.evaluate_market(market, now, set())
    assert result["pass"] is True, result
    assert result["entry_payload"]["cluster_auto"] == "A_SPORTS"
    assert result["entry_payload"]["no_token_id"] == "no_t"
    assert result["entry_payload"]["uma_risk_flag"] == 0
    assert abs(result["entry_payload"]["no_best_ask_at_entry"] - 0.90) < 1e-6
    print("✅ test_filter_chain_pass")
    return market, fake_book


def test_filter_chain_rejects():
    now = datetime.now(timezone.utc)
    end_ok = now + timedelta(hours=36)

    # Crypto rejection
    crypto = {
        "id": "m_crypto",
        "slug": "btc-100k-by-may",
        "question": "Will Bitcoin hit $100k by May?",
        "tags": [{"label": "Crypto"}],
        "endDate": end_ok.isoformat(),
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps(["y", "n"]),
        "enableOrderBook": True,
    }
    r = discovery.evaluate_market(crypto, now, set())
    assert not r["pass"] and r["reject_reason"] == "crypto_blacklist", r

    # TTR out of window
    too_far = dict(crypto, slug="too-far", question="Foo bar", tags=[])
    too_far["endDate"] = (now + timedelta(hours=200)).isoformat()
    r = discovery.evaluate_market(too_far, now, set())
    assert not r["pass"] and "ttr_out_of_window" in r["reject_reason"], r

    # Closed
    closed = dict(crypto, slug="closed-mkt", question="Foo", tags=[], closed=True)
    r = discovery.evaluate_market(closed, now, set())
    assert not r["pass"] and r["reject_reason"] == "closed_or_archived", r

    # Already entered
    entered_id = "m_already"
    already = {
        "id": entered_id, "slug": "x", "question": "x", "tags": [],
        "endDate": end_ok.isoformat(), "enableOrderBook": True,
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps(["y", "n"]),
    }
    r = discovery.evaluate_market(already, now, {entered_id})
    assert not r["pass"] and r["reject_reason"] == "already_entered", r
    print("✅ test_filter_chain_rejects")


def test_uma_flag_does_not_block():
    """UMA dispute marker should FLAG but not block (HR9)."""
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=36)
    market = {
        "id": "m_geo",
        "slug": "ceasefire-may",
        "question": "Will there be a ceasefire by May 7?",
        "tags": [{"label": "Geopolitics"}],
        "endDate": end.isoformat(),
        "enableOrderBook": True,
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps(["y", "n"]),
        "resolutionSource": "Resolved by consensus of credible reporting from major outlets.",
    }
    fake_book = {"asks": [{"price": "0.92", "size": "500"}], "bids": [{"price": "0.90", "size": "500"}]}
    with patch.object(discovery, "fetch_orderbook", return_value=fake_book):
        result = discovery.evaluate_market(market, now, set())
    assert result["pass"] is True, "UMA flag must not block"
    assert result["entry_payload"]["uma_risk_flag"] == 1, "UMA flag must be set"
    print("✅ test_uma_flag_does_not_block")


def test_position_lifecycle_resolved_no_win():
    """Full lifecycle: entry → price update → resolution NO wins → P&L correct."""
    # Reset state dir
    for f in [config.POSITIONS_CSV, config.DISCOVERY_CSV, config.PRICELOG_CSV]:
        if os.path.exists(f):
            os.remove(f)
    position.init_state()

    payload = {
        "market_id": "lifecycle_1",
        "slug": "test-slug",
        "question": "Test question",
        "cluster_auto": "A_SPORTS",
        "ttr_at_entry_hours": 36.0,
        "no_token_id": "no_tok",
        "yes_token_id": "yes_tok",
        "no_best_ask_at_entry": 0.90,
        "no_best_bid_at_entry": 0.88,
        "volume_at_entry": 100000,
        "book_depth_no_top3": 1500.0,
        "uma_risk_flag": 0,
        "end_date_iso": (datetime.now(timezone.utc) + timedelta(hours=36)).isoformat(),
    }
    row = position.create_entry(payload)
    # Fill price = 0.90 + 0.005 = 0.905, shares = 1.0/0.905 ≈ 1.10497
    assert abs(float(row["simulated_fill_price"]) - 0.905) < 1e-6
    assert abs(float(row["simulated_shares"]) - (1.0 / 0.905)) < 1e-4

    # Re-entry blocked
    entered = position.all_entered_market_ids()
    assert "lifecycle_1" in entered

    # Price update
    position.update_open_price("lifecycle_1", 0.93, 0.91, 30.0)
    found = position.find_position("lifecycle_1")
    assert found["current_no_ask"] == "0.93"

    # Resolve NO win → exit at 1.00 → pnl ≈ 1.0 - 0.905 = 0.095 * 1.10497 ≈ 0.10497
    resolved = position.resolve_position("lifecycle_1", no_won=True)
    expected_pnl = (1.0 - 0.905) * (1.0 / 0.905)
    assert abs(float(resolved["pnl"]) - expected_pnl) < 1e-4, (resolved["pnl"], expected_pnl)
    assert resolved["status"] == "RESOLVED_NO"
    print(f"✅ test_position_lifecycle_resolved_no_win  pnl=${float(resolved['pnl']):.4f}")


def test_position_lifecycle_yes_loss():
    payload = {
        "market_id": "lifecycle_loss",
        "slug": "test-loss",
        "question": "Test loss",
        "cluster_auto": "B_MENTION",
        "ttr_at_entry_hours": 48.0,
        "no_token_id": "no_tok2",
        "yes_token_id": "yes_tok2",
        "no_best_ask_at_entry": 0.90,
        "no_best_bid_at_entry": 0.88,
        "volume_at_entry": 50000,
        "book_depth_no_top3": 800.0,
        "uma_risk_flag": 0,
        "end_date_iso": (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat(),
    }
    position.create_entry(payload)
    resolved = position.resolve_position("lifecycle_loss", no_won=False)
    # exit 0.0, fill 0.905 → pnl = -0.905 * shares ≈ -1.0
    assert float(resolved["pnl"]) < -0.99, resolved["pnl"]
    assert resolved["status"] == "RESOLVED_YES"
    print(f"✅ test_position_lifecycle_yes_loss  pnl=${float(resolved['pnl']):.4f}")


def test_manual_sell():
    payload = {
        "market_id": "manual_sell_1",
        "slug": "manual-test",
        "question": "Manual sell test",
        "cluster_auto": "C_WEATHER",
        "ttr_at_entry_hours": 30.0,
        "no_token_id": "no_tok3",
        "yes_token_id": "yes_tok3",
        "no_best_ask_at_entry": 0.90,
        "no_best_bid_at_entry": 0.88,
        "volume_at_entry": 25000,
        "book_depth_no_top3": 200.0,
        "uma_risk_flag": 0,
        "end_date_iso": (datetime.now(timezone.utc) + timedelta(hours=30)).isoformat(),
    }
    position.create_entry(payload)
    # Update price so current_no_bid is set
    position.update_open_price("manual_sell_1", 0.93, 0.91, 25.0)
    sold = position.manual_sell("manual_sell_1")
    assert sold is not None
    assert sold["status"] == "MANUAL_SOLD"
    # Exit price = 0.91 - 0.005 = 0.905 → PnL = (0.905 - 0.905) * shares ≈ 0
    assert abs(float(sold["pnl"])) < 1e-3, f"Expected near-zero PnL, got {sold['pnl']}"

    # Second manual sell on same market should be no-op
    again = position.manual_sell("manual_sell_1")
    assert again is None
    print(f"✅ test_manual_sell  pnl=${float(sold['pnl']):.4f}")


def test_reclassify():
    payload = {
        "market_id": "reclass_1",
        "slug": "reclass-test", "question": "Recl test",
        "cluster_auto": "H_OTHER", "ttr_at_entry_hours": 30.0,
        "no_token_id": "n", "yes_token_id": "y",
        "no_best_ask_at_entry": 0.90, "no_best_bid_at_entry": 0.88,
        "volume_at_entry": 1000, "book_depth_no_top3": 100,
        "uma_risk_flag": 0,
        "end_date_iso": (datetime.now(timezone.utc) + timedelta(hours=30)).isoformat(),
    }
    position.create_entry(payload)
    ok = position.reclassify("reclass_1", "F_POLITICS")
    assert ok
    found = position.find_position("reclass_1")
    assert found["cluster_auto"] == "H_OTHER"  # preserved
    assert found["cluster_override"] == "F_POLITICS"
    print("✅ test_reclassify")


def test_summary():
    s = position.compute_summary()
    print(f"   Summary: {s}")
    assert s["total"] >= 4  # we created 4 above
    assert s["pnl_usd"] != 0
    s_scaled = position.compute_summary(scaled=True)
    # Scaling is cosmetic; small rounding artifact from 4-dp display rounding is acceptable.
    assert abs(s_scaled["pnl_usd"] - s["pnl_usd"] * 100.0) < 0.5
    print("✅ test_summary")


def test_dashboard_routes():
    """Smoke-test Flask routes return 200."""
    import dashboard as dash
    dash.app.config["TESTING"] = True
    client = dash.app.test_client()

    r = client.get("/")
    assert r.status_code == 200
    assert b"Nostradamus" in r.data

    r = client.get("/history")
    assert r.status_code == 200

    r = client.get("/discovery")
    assert r.status_code == 200

    r = client.get("/healthz")
    assert r.status_code == 200
    j = r.get_json()
    assert "version" in j

    # Sell on non-existent market -> 404
    r = client.post("/sell/nonexistent")
    assert r.status_code == 404

    # Reclassify with bad cluster -> 400
    r = client.post("/reclassify/reclass_1", data={"cluster": "BAD"})
    assert r.status_code == 400

    # Reclassify with good cluster -> redirect (302)
    r = client.post("/reclassify/reclass_1", data={"cluster": "G_TECH_CORP"})
    assert r.status_code in (302, 303)

    print("✅ test_dashboard_routes")


def test_nan_guard():
    """Iron rule: required cols can never be silently empty."""
    bad_payload = {
        "market_id": "nan_test",
        "slug": "nan",
        "question": "",
        "cluster_auto": "A_SPORTS",
        "ttr_at_entry_hours": 30.0,
        "no_token_id": "",  # EMPTY — should fail
        "yes_token_id": "y",
        "no_best_ask_at_entry": 0.90,
        "no_best_bid_at_entry": 0.88,
        "volume_at_entry": 1000,
        "book_depth_no_top3": 100,
        "uma_risk_flag": 0,
        "end_date_iso": (datetime.now(timezone.utc) + timedelta(hours=30)).isoformat(),
    }
    try:
        position.create_entry(bad_payload)
        assert False, "Expected ValueError on empty no_token_id"
    except ValueError as e:
        assert "no_token_id" in str(e)
    print("✅ test_nan_guard")


# ===== Verification chain tests =====

def test_resolve_immutability():
    """Once resolved, resolve_position must refuse to overwrite."""
    payload = {
        "market_id": "immut_1", "slug": "s", "question": "q",
        "cluster_auto": "A_SPORTS", "ttr_at_entry_hours": 30.0,
        "no_token_id": "n", "yes_token_id": "y",
        "no_best_ask_at_entry": 0.90, "no_best_bid_at_entry": 0.88,
        "volume_at_entry": 1000, "book_depth_no_top3": 100, "uma_risk_flag": 0,
        "end_date_iso": (datetime.now(timezone.utc) + timedelta(hours=30)).isoformat(),
    }
    position.create_entry(payload)
    r1 = position.resolve_position("immut_1", no_won=True)
    assert r1["status"] == "RESOLVED_NO"
    # Try to flip it
    r2 = position.resolve_position("immut_1", no_won=False)
    assert r2 is None, "Resolved positions must be immutable"
    found = position.find_position("immut_1")
    assert found["status"] == "RESOLVED_NO", "Status must not have changed"
    print("✅ test_resolve_immutability")


def test_parse_resolution_clear_winner():
    import main
    market = {
        "closed": True,
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.0", "1.0"]),
    }
    parsed = main._parse_resolution_from_market(market)
    assert parsed["winner"] == "no", parsed
    assert parsed["gamma_closed"] is True
    print("✅ test_parse_resolution_clear_winner")


def test_parse_resolution_unclear_blocks():
    """Failure mode #1: phantom resolution. Both prices ≈ 0.5 must NOT yield a winner."""
    import main
    cases = [
        # Both ~50/50
        {"closed": True, "outcomes": json.dumps(["Yes", "No"]),
         "outcomePrices": json.dumps(["0.5", "0.5"])},
        # Empty prices
        {"closed": True, "outcomes": json.dumps(["Yes", "No"]),
         "outcomePrices": json.dumps([])},
        # Missing field
        {"closed": True, "outcomes": json.dumps(["Yes", "No"])},
        # Don't sum to 1
        {"closed": True, "outcomes": json.dumps(["Yes", "No"]),
         "outcomePrices": json.dumps(["0.3", "0.3"])},
        # Below threshold (0.95 < 0.99)
        {"closed": True, "outcomes": json.dumps(["Yes", "No"]),
         "outcomePrices": json.dumps(["0.05", "0.95"])},
    ]
    for i, m in enumerate(cases):
        parsed = main._parse_resolution_from_market(m)
        assert parsed["winner"] == "unclear", f"case {i} should be unclear: {parsed}"
    print("✅ test_parse_resolution_unclear_blocks")


def test_token_alignment_check():
    """Failure mode #3: reversed resolution via token-id misalignment."""
    import main
    # Market where "No" is at index 0, no_token_id="N1"
    market = {
        "outcomes": json.dumps(["No", "Yes"]),
        "clobTokenIds": json.dumps(["N1", "Y1"]),
    }
    assert main._verify_token_alignment(market, "N1") is True
    # If we'd stored Y1 as no_token_id, alignment check must fail
    assert main._verify_token_alignment(market, "Y1") is False
    # Bad data
    assert main._verify_token_alignment({}, "N1") is False
    print("✅ test_token_alignment_check")


def test_two_poll_verification_happy_path():
    """End-to-end: poll 1 → AWAITING_2ND, poll 2 (after delay) → RESOLVED."""
    import main
    # Reset state
    for f in [config.POSITIONS_CSV, config.RESOLUTIONS_CSV]:
        if os.path.exists(f):
            os.remove(f)
    position.init_state()

    payload = {
        "market_id": "tp_1", "slug": "tp-slug", "question": "TP test",
        "cluster_auto": "A_SPORTS", "ttr_at_entry_hours": 30.0,
        "no_token_id": "NTOK", "yes_token_id": "YTOK",
        "no_best_ask_at_entry": 0.90, "no_best_bid_at_entry": 0.88,
        "volume_at_entry": 1000, "book_depth_no_top3": 100, "uma_risk_flag": 0,
        # End date 2hrs ago so RESOLUTION_MIN_AGE_S (1hr) is satisfied
        "end_date_iso": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
    }
    position.create_entry(payload)

    fake_market = {
        "id": "tp_1",
        "closed": True,
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.0", "1.0"]),
        "clobTokenIds": json.dumps(["YTOK", "NTOK"]),
    }
    p = position.find_position("tp_1")
    now = datetime.now(timezone.utc)

    # Poll #1 — should set AWAITING_2ND
    with patch.object(main, "discovery") as mock_disc:
        mock_disc.fetch_market_by_id.return_value = fake_market
        # parse_outcomes_and_tokens still needs to work
        import discovery as real_disc
        mock_disc.parse_outcomes_and_tokens.side_effect = real_disc.parse_outcomes_and_tokens
        main._process_one_resolution(p, now)
    p2 = position.find_position("tp_1")
    assert p2["status"] == "RESOLUTION_AWAITING_2ND", p2["status"]
    assert p2["verify_first_poll_winner"] == "no"

    # Poll #2 — too soon → no change
    p3 = position.find_position("tp_1")
    with patch.object(main, "discovery") as mock_disc:
        mock_disc.fetch_market_by_id.return_value = fake_market
        import discovery as real_disc
        mock_disc.parse_outcomes_and_tokens.side_effect = real_disc.parse_outcomes_and_tokens
        main._process_one_resolution(p3, now + timedelta(seconds=10))
    p4 = position.find_position("tp_1")
    assert p4["status"] == "RESOLUTION_AWAITING_2ND", "Too-early 2nd poll must not resolve"

    # Poll #2 — after confirm delay → RESOLVED
    p5 = position.find_position("tp_1")
    with patch.object(main, "discovery") as mock_disc:
        mock_disc.fetch_market_by_id.return_value = fake_market
        import discovery as real_disc
        mock_disc.parse_outcomes_and_tokens.side_effect = real_disc.parse_outcomes_and_tokens
        main._process_one_resolution(
            p5, now + timedelta(seconds=config.RESOLUTION_CONFIRM_DELAY_S + 5))
    p6 = position.find_position("tp_1")
    assert p6["status"] == "RESOLVED_NO", p6["status"]
    assert float(p6["pnl"]) > 0
    print(f"✅ test_two_poll_verification_happy_path  pnl=${float(p6['pnl']):.4f}")


def test_two_poll_verification_disputed():
    """Failure mode #4: stale read. Polls disagree → DISPUTED, never auto-resolve."""
    import main
    payload = {
        "market_id": "tp_disp", "slug": "tp-disp", "question": "Dispute test",
        "cluster_auto": "B_MENTION", "ttr_at_entry_hours": 30.0,
        "no_token_id": "NTOK2", "yes_token_id": "YTOK2",
        "no_best_ask_at_entry": 0.90, "no_best_bid_at_entry": 0.88,
        "volume_at_entry": 1000, "book_depth_no_top3": 100, "uma_risk_flag": 0,
        "end_date_iso": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
    }
    position.create_entry(payload)

    market_no_wins = {
        "id": "tp_disp", "closed": True,
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.0", "1.0"]),
        "clobTokenIds": json.dumps(["YTOK2", "NTOK2"]),
    }
    market_yes_wins = {
        "id": "tp_disp", "closed": True,
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["1.0", "0.0"]),
        "clobTokenIds": json.dumps(["YTOK2", "NTOK2"]),
    }
    p = position.find_position("tp_disp")
    now = datetime.now(timezone.utc)
    import discovery as real_disc

    # Poll #1: NO wins
    with patch.object(main, "discovery") as mock_disc:
        mock_disc.fetch_market_by_id.return_value = market_no_wins
        mock_disc.parse_outcomes_and_tokens.side_effect = real_disc.parse_outcomes_and_tokens
        main._process_one_resolution(p, now)
    p2 = position.find_position("tp_disp")
    assert p2["status"] == "RESOLUTION_AWAITING_2ND"

    # Poll #2: YES wins (disagreement!)
    with patch.object(main, "discovery") as mock_disc:
        mock_disc.fetch_market_by_id.return_value = market_yes_wins
        mock_disc.parse_outcomes_and_tokens.side_effect = real_disc.parse_outcomes_and_tokens
        main._process_one_resolution(
            p2, now + timedelta(seconds=config.RESOLUTION_CONFIRM_DELAY_S + 5))
    p3 = position.find_position("tp_disp")
    assert p3["status"] == "RESOLUTION_DISPUTED", p3["status"]
    assert p3.get("pnl") in (None, ""), "Disputed must not auto-book PnL"
    print("✅ test_two_poll_verification_disputed")


def test_token_misalignment_blocks_resolution():
    """Failure mode #3: misaligned token must never resolve, even if winner is clear."""
    import main
    payload = {
        "market_id": "tp_mis", "slug": "tp-mis", "question": "Misalign test",
        "cluster_auto": "C_WEATHER", "ttr_at_entry_hours": 30.0,
        # We stored "FAKE_NO_TOK" as no_token_id at entry
        "no_token_id": "FAKE_NO_TOK", "yes_token_id": "Y_OK",
        "no_best_ask_at_entry": 0.90, "no_best_bid_at_entry": 0.88,
        "volume_at_entry": 1000, "book_depth_no_top3": 100, "uma_risk_flag": 0,
        "end_date_iso": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
    }
    position.create_entry(payload)
    # But Gamma now reports the No token as "REAL_NO_TOK" — alignment broken
    market = {
        "id": "tp_mis", "closed": True,
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.0", "1.0"]),
        "clobTokenIds": json.dumps(["Y_OK", "REAL_NO_TOK"]),
    }
    p = position.find_position("tp_mis")
    now = datetime.now(timezone.utc)
    import discovery as real_disc
    with patch.object(main, "discovery") as mock_disc:
        mock_disc.fetch_market_by_id.return_value = market
        mock_disc.parse_outcomes_and_tokens.side_effect = real_disc.parse_outcomes_and_tokens
        main._process_one_resolution(p, now)
    p2 = position.find_position("tp_mis")
    assert p2["status"] == "RESOLUTION_DISPUTED", \
        f"Token misalign must yield DISPUTED, got {p2['status']}"
    print("✅ test_token_misalignment_blocks_resolution")


def test_premature_resolution_blocked():
    """Failure mode #2: market past end_date but Gamma not yet closed → no resolve."""
    import main
    payload = {
        "market_id": "tp_premat", "slug": "tp-premat", "question": "Prem test",
        "cluster_auto": "D_BOXOFFICE", "ttr_at_entry_hours": 30.0,
        "no_token_id": "N", "yes_token_id": "Y",
        "no_best_ask_at_entry": 0.90, "no_best_bid_at_entry": 0.88,
        "volume_at_entry": 1000, "book_depth_no_top3": 100, "uma_risk_flag": 0,
        "end_date_iso": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
    }
    position.create_entry(payload)
    market_not_closed = {
        "id": "tp_premat", "closed": False, "resolved": False,
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.05", "0.95"]),
        "clobTokenIds": json.dumps(["Y", "N"]),
    }
    p = position.find_position("tp_premat")
    now = datetime.now(timezone.utc)
    import discovery as real_disc
    with patch.object(main, "discovery") as mock_disc:
        mock_disc.fetch_market_by_id.return_value = market_not_closed
        mock_disc.parse_outcomes_and_tokens.side_effect = real_disc.parse_outcomes_and_tokens
        main._process_one_resolution(p, now)
    p2 = position.find_position("tp_premat")
    assert p2["status"] == "OPEN", f"Not-closed must stay OPEN, got {p2['status']}"
    print("✅ test_premature_resolution_blocked")


def test_manual_resolve_after_dispute():
    """After DISPUTED, human can manually resolve via dashboard."""
    # Reuse tp_disp from earlier dispute test
    p = position.find_position("tp_disp")
    assert p and p["status"] == "RESOLUTION_DISPUTED"
    result = position.resolve_position("tp_disp", no_won=True, manual=True)
    assert result is not None
    assert result["status"] == "MANUAL_RESOLVED_NO"
    assert float(result["pnl"]) > 0
    # And it's now immutable
    again = position.resolve_position("tp_disp", no_won=False, manual=True)
    assert again is None
    print(f"✅ test_manual_resolve_after_dispute  pnl=${float(result['pnl']):.4f}")


def test_resolutions_csv_audit_trail():
    """Every resolution attempt must leave an audit row."""
    rows = position._read_all(config.RESOLUTIONS_CSV, position.RESOLUTION_COLS)
    assert len(rows) > 0, "Resolutions CSV must have audit rows"
    actions = {r["action_taken"] for r in rows}
    # Should include at least these actions from the tests above
    assert "FIRST_POLL_RECORDED" in actions, actions
    print(f"✅ test_resolutions_csv_audit_trail  ({len(rows)} audit rows, actions={actions})")


def test_pending_verify_route():
    """Dashboard /pending-verify must render."""
    import dashboard as dash
    client = dash.app.test_client()
    r = client.get("/pending-verify")
    assert r.status_code == 200
    assert b"Verify" in r.data
    print("✅ test_pending_verify_route")


def test_manual_resolve_route():
    """POST /manual-resolve/<id> with bad winner → 400; good → 302."""
    import dashboard as dash
    client = dash.app.test_client()
    # Bad winner
    r = client.post("/manual-resolve/nonexistent", data={"winner": "MAYBE"})
    assert r.status_code == 400
    # Nonexistent market (good winner)
    r = client.post("/manual-resolve/nonexistent", data={"winner": "NO"})
    assert r.status_code == 404
    print("✅ test_manual_resolve_route")


# Run all
if __name__ == "__main__":
    test_blacklist()
    test_cluster()
    test_outcome_parsing()
    test_book_parsing()
    test_filter_chain_pass()
    test_filter_chain_rejects()
    test_uma_flag_does_not_block()
    test_position_lifecycle_resolved_no_win()
    test_position_lifecycle_yes_loss()
    test_manual_sell()
    test_reclassify()
    test_summary()
    test_dashboard_routes()
    test_nan_guard()
    # Verification chain tests
    test_resolve_immutability()
    test_parse_resolution_clear_winner()
    test_parse_resolution_unclear_blocks()
    test_token_alignment_check()
    test_two_poll_verification_happy_path()
    test_two_poll_verification_disputed()
    test_token_misalignment_blocks_resolution()
    test_premature_resolution_blocked()
    test_manual_resolve_after_dispute()
    test_resolutions_csv_audit_trail()
    test_pending_verify_route()
    test_manual_resolve_route()
    shutil.rmtree(TMP, ignore_errors=True)
    print("\n🎉 ALL TESTS PASSED")
