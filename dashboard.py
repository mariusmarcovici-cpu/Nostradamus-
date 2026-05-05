"""Flask dashboard. Public, no auth (per Marius decision).

Routes:
  GET  /          — overview + open positions table
  GET  /open      — same as / for now
  GET  /history   — closed positions
  GET  /discovery — last 200 rows from discovery.csv
  POST /sell/<market_id>      — manual sell
  POST /reclassify/<market_id> — flag misclassification
  GET  /healthz   — heartbeat + open count
"""

import csv
import logging
import os
from datetime import datetime, timezone

from flask import Flask, render_template, request, redirect, url_for, jsonify

import config
import position
from cluster import CLUSTERS

log = logging.getLogger(__name__)
app = Flask(__name__, template_folder="templates")


def _ttr_remaining(end_iso: str) -> str:
    if not end_iso:
        return ""
    try:
        end = datetime.fromisoformat(end_iso)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        delta = (end - datetime.now(timezone.utc)).total_seconds()
        if delta < 0:
            return f"-{abs(delta)/3600:.1f}h"
        if delta < 3600:
            return f"{delta/60:.0f}m"
        return f"{delta/3600:.1f}h"
    except (ValueError, TypeError):
        return ""


def _delta_pct(row: dict) -> str:
    try:
        entry = float(row["simulated_fill_price"])
        cur_ask = row.get("current_no_ask")
        if cur_ask in (None, ""):
            return ""
        cur = float(cur_ask)
        return f"{(cur - entry):+.3f}"
    except (TypeError, ValueError, KeyError):
        return ""


@app.route("/")
def index():
    scaled = request.args.get("scaled") == "1"
    summary = position.compute_summary(scaled=scaled)
    rows = position.open_positions()
    # Decorate rows for display
    decorated = []
    for r in rows:
        d = dict(r)
        d["ttr_remaining"] = _ttr_remaining(r.get("end_date_iso", ""))
        d["delta_price"] = _delta_pct(r)
        d["polymarket_url"] = f"https://polymarket.com/event/{r.get('slug', '')}"
        d["display_cluster"] = r.get("cluster_override") or r.get("cluster_auto") or ""
        decorated.append(d)
    # Sort by TTR ascending (closest to resolution first)
    decorated.sort(key=lambda x: x.get("end_date_iso") or "9999")
    return render_template(
        "dashboard.html",
        summary=summary,
        rows=decorated,
        clusters=CLUSTERS,
        view="open",
        scaled=scaled,
        version=config.VERSION,
    )


@app.route("/history")
def history():
    scaled = request.args.get("scaled") == "1"
    summary = position.compute_summary(scaled=scaled)
    rows = position.all_positions()
    closed = [r for r in rows if r.get("status") not in ("OPEN", "")]
    decorated = []
    for r in closed:
        d = dict(r)
        d["display_cluster"] = r.get("cluster_override") or r.get("cluster_auto") or ""
        d["polymarket_url"] = r.get("polymarket_url") or f"https://polymarket.com/event/{r.get('slug', '')}"
        decorated.append(d)
    decorated.sort(key=lambda x: x.get("exit_ts") or "", reverse=True)
    return render_template(
        "dashboard.html",
        summary=summary,
        rows=decorated,
        clusters=CLUSTERS,
        view="history",
        scaled=scaled,
        version=config.VERSION,
    )


@app.route("/pending-verify")
def pending_verify():
    """Positions that need human review: stuck in verification, UMA pending, or disputed."""
    rows = position.all_positions()
    pending = [r for r in rows if r.get("status") in
               ("RESOLUTION_PENDING_VERIFY", "RESOLUTION_AWAITING_2ND",
                "UMA_PENDING", "RESOLUTION_DISPUTED")]
    decorated = []
    for r in pending:
        d = dict(r)
        d["display_cluster"] = r.get("cluster_override") or r.get("cluster_auto") or ""
        d["polymarket_url"] = r.get("polymarket_url") or f"https://polymarket.com/event/{r.get('slug', '')}"
        decorated.append(d)
    decorated.sort(key=lambda x: x.get("end_date_iso") or "")
    summary = position.compute_summary()
    return render_template(
        "pending_verify.html",
        rows=decorated,
        summary=summary,
        version=config.VERSION,
    )


@app.route("/manual-resolve/<market_id>", methods=["POST"])
def manual_resolve(market_id):
    """Human override: mark a stuck position resolved as NO or YES."""
    winner = (request.form.get("winner") or "").strip().upper()
    if winner not in ("NO", "YES"):
        return f"Bad winner '{winner}', must be NO or YES", 400
    no_won = (winner == "NO")
    result = position.resolve_position(market_id, no_won=no_won, manual=True)
    if result is None:
        return f"Position {market_id} not found or already resolved", 404
    return redirect(request.referrer or url_for("pending_verify"))


@app.route("/discovery")
def discovery_view():
    rows = []
    if os.path.exists(config.DISCOVERY_CSV):
        with open(config.DISCOVERY_CSV) as f:
            rows = list(csv.DictReader(f))
    rows = rows[-200:]
    rows.reverse()
    return render_template(
        "discovery.html",
        rows=rows,
        version=config.VERSION,
    )


@app.route("/sell/<market_id>", methods=["POST"])
def sell(market_id):
    result = position.manual_sell(market_id)
    if result is None:
        return f"Position {market_id} not found or not open", 404
    return redirect(url_for("index"))


@app.route("/reclassify/<market_id>", methods=["POST"])
def reclassify(market_id):
    new_cluster = request.form.get("cluster", "").strip()
    if new_cluster not in CLUSTERS:
        return f"Invalid cluster {new_cluster}", 400
    ok = position.reclassify(market_id, new_cluster)
    if not ok:
        return f"Position {market_id} not found", 404
    return redirect(request.referrer or url_for("index"))


@app.route("/healthz")
def healthz():
    hb = ""
    if os.path.exists(config.HEARTBEAT_FILE):
        with open(config.HEARTBEAT_FILE) as f:
            hb = f.read().strip()
    return jsonify({
        "version": config.VERSION,
        "heartbeat": hb,
        "open_positions": len(position.open_positions()),
        "now": datetime.now(timezone.utc).isoformat(),
    })


def run(port: int):
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
