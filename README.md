# SentiBot v0.1.0-dry

Polymarket non-crypto sentiment bot. Identifies markets resolving in **24–70 hours** with **NO at 85–95¢**, paper-buys at $1/trade, holds to resolution by default with manual sell available via dashboard.

## What it does

- Polls Polymarket Gamma API every 60s for markets in the TTR window
- Filters out crypto via slug/tag/regex triple-check
- Flags (does NOT block) UMA-dispute-prone markets
- Classifies into clusters A–H (sports, mention, weather, box office, geopolitics, politics-other, tech/corp, other)
- Paper-buys NO at best ask + 0.5¢ slippage, $1 per trade
- Refreshes prices every 30s (5s in final hour)
- Auto-resolves on Polymarket close, OR manual sell via dashboard button
- Reclassify button on dashboard to fix misclassified clusters

## Hard rules (do not loosen without re-spec)

1. DRY MODE ONLY. No real orders. No wallet calls.
2. Flat $1/trade.
3. One entry per market_id, ever.
4. Default = hold to resolution.
5. Manual sell is the only early-exit path.

## Run locally

```bash
pip install -r requirements.txt
python main.py
# Open http://localhost:8080
```

## Deploy to Railway

1. Push to GitHub repo `sentibot`
2. New Railway project from repo
3. **Add a volume mount on the service** with mount path `/app/state` (so CSVs persist across redeploys — without this, all paper positions are lost on each deploy)
4. Railway sets `$PORT` automatically; healthcheck is at `/healthz`
5. Verify within 90s of deploy:
   - `curl https://<service>.up.railway.app/healthz` returns version + heartbeat
   - Logs show `SentiBot 0.1.0-dry starting up — DRY MODE`
   - No Traceback in last 60s

## File layout

```
main.py            # entry, three background loops + Flask
discovery.py       # Gamma + CLOB client, filter chain
cluster.py         # heuristic cluster classifier
position.py        # paper position lifecycle, CSV persistence
dashboard.py       # Flask routes
blacklists.py      # crypto patterns + UMA dispute keywords
config.py          # all tunables
templates/
  dashboard.html
  discovery.html
state/             # CSVs (volume-mounted in production)
  sentibot_positions.csv
  sentibot_discovery.csv
  sentibot_pricelog.csv
  heartbeat.txt
```

## CSV schemas

See `position.py` constants `POSITION_COLS`, `DISCOVERY_COLS`, `PRICELOG_COLS`.

## Dashboard

- **/** — open positions, with Sell + Reclassify buttons
- **/history** — closed positions with PnL
- **/discovery** — last 200 markets evaluated, pass/fail with reason
- **/healthz** — JSON heartbeat for Railway healthcheck

Toggle "View as if $100/trade" multiplies displayed PnL by 100 for readability while keeping underlying numbers at $1.

## Known limitations (v1)

- Resolution detection assumes Polymarket's `outcomePrices` field is populated post-close. If it's not, position stays OPEN until UMA window passes (4hr) then flips to UMA_PENDING (manual review).
- Cluster classifier ~5–10% misclassification rate. Use Reclassify button to correct; both auto and override are preserved for accuracy stats.
- No alerting (email/SMS/Slack). Dashboard only.
- Single-process. Multi-process deploys would need fcntl on CSV writes.
