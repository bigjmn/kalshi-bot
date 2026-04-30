# kalshi-btc

Automated two-sided trading bot for Kalshi BTC 15-minute prediction markets.

The bot computes the theoretical fair value of YES and NO contracts using an Arithmetic Brownian Motion (ABM) model, then places limit orders whenever the market ask price offers sufficient edge over the model price.

---

## How it works

### Market structure

Kalshi KXBTC15M markets settle YES at $1 if the **mean BTC price over the final 60 seconds** of the 15-minute window exceeds a floor strike `K`. The bot monitors these markets in real time and bets when it detects mispricing.

### Probability model

The bot models BTC price as a driftless ABM: `dX = σ dW`.

Two regimes based on time remaining (`τ = T - t`):

**Before the settlement window** (`τ ≥ 60s`):
```
m_t = X_t
v_t = σ² · (τ - 2W/3)
p_yes = Φ((m_t - K) / √v_t)
```

**Inside the settlement window** (`0 < τ < 60s`):
```
m_t = (A_t + X_t · τ) / W
v_t = σ² · τ³ / (3W²)
p_yes = Φ((m_t - K) / √v_t)
```

Where `A_t = ∫[T-W, t] X_s ds` is the realized price integral (computed via trapezoid rule from live BTC tick data), and `W = 60s`.

### Volatility (σ)

`σ` is estimated as `std(price_diffs) / √(mean_dt)` from the last 30 minutes of BTC tick data stored in `btc_reference.jsonl`. On fresh startup with no historical data, it falls back to `DEFAULT_SIGMA_FALLBACK = 10.0` USD/√s (consistent with ~70% annualized BTC volatility at ~$75k).

### Edge and sizing

A bet is placed when:
```
p_yes - best_yes_ask ≥ EDGE_THRESHOLD   → buy YES
p_no  - best_no_ask  ≥ EDGE_THRESHOLD   → buy NO
```

Contract count is determined by fractional Kelly:
```
f = kelly_fraction · (p - a) / (1 - a)
contracts = floor(f · balance / a),  min 1
```

At most one YES and one NO position is held per market ticker.

### Cashout

If `CASHOUT_DELTA` is set (e.g. `0.25`), the bot automatically sells a position when the market ask for that side rises `CASHOUT_DELTA` above the entry price. After a successful cashout, re-entry is allowed. Set to `None` (default) to hold until expiry.

---

## Configuration

All settings are loaded from a `.env` file:

| Variable | Description | Default |
|---|---|---|
| `KALSHI_KEY_ID` | Kalshi API key ID | required |
| `KALSHI_PRIVATE_KEY_PATH` | Path to RSA private key file | required |
| `KALSHI_ENV` | `prod` or `demo` | required |
| `KALSHI_OUTPUT_DIR` | Directory for data output | `./data` |
| `KALSHI_SNAPSHOT_INTERVAL_SEC` | Order book snapshot frequency | `1.0` |
| `KALSHI_REST_SEED` | Seed order book from REST before WS | `True` |
| `KALSHI_DISCOVERY_LOOKAHEAD_MIN` | How far ahead to discover markets | `900` |
| `KALSHI_KELLY_FRACTION` | Kelly fraction (1.0 = full Kelly) | `1.0` |

Trading behavior is controlled by constants at the top of `trading_bot.py`:

| Constant | Description | Default |
|---|---|---|
| `EDGE_THRESHOLD` | Minimum edge (in probability) to place a bet | `0.12` |
| `DEFAULT_KELLY_FRACTION` | Default Kelly fraction | `1.0` |
| `DEFAULT_SIGMA_FALLBACK` | σ used when no historical data exists | `10.0` |
| `CASHOUT_DELTA` | Auto-sell when ask rises this much above entry; `None` = hold | `None` |
| `STATUS_LOG_INTERVAL_SEC` | Frequency of status log lines | `20.0` |

---

## Files

```
main.py                        — entry point: wires all components together
trading_bot.py                 — KalshiTrader: probability model, sizing, order placement
kalshi_orderbook_collector.py  — WebSocket order book collector + market discovery
price_tracker.py               — polls Kalshi's BTC reference price feed every second
true_prob.py                   — ABM yes_probability() formula
setup.sh                       — install script (uv sync + optional systemd service)
cloud-startup.sh               — GCP VM startup script (clones repo, fetches secrets, starts service)
```

### Data output

```
data/
  trade_log.jsonl                          — every order attempt (buy and sell), with response
  markets_ref/
    btc_reference.jsonl                    — BTC tick data (used for σ estimation)
    {ticker}/
      events.jsonl                         — raw WebSocket events per market
      book_states.jsonl                    — order book snapshots (1/sec)
```

---

## Setup

### Local

```bash
# Install dependencies and configure systemd (Linux) or print run command (macOS)
./setup.sh
```

Requires a `.env` file in the project root. See Configuration above.

To run directly:
```bash
uv run python main.py
```

### GCP (recommended for production)

The bot is designed to run continuously on a GCP e2-micro instance (always-free tier).

**One-time setup from your local machine:**

```bash
# 1. Store secrets in Secret Manager
echo -n "YOUR_KEY_ID" | gcloud secrets create kalshi-key-id --data-file=-
gcloud secrets create kalshi-private-key --data-file="/path/to/private_key.pem"
gcloud secrets create github-deploy-key --data-file="$HOME/.ssh/your_github_key"

# 2. Grant the VM's service account access
SA=$(gcloud iam service-accounts list \
  --filter="displayName:Compute Engine default" \
  --format="value(email)")
for SECRET in kalshi-key-id kalshi-private-key github-deploy-key; do
  gcloud secrets add-iam-policy-binding $SECRET \
    --member="serviceAccount:$SA" --role="roles/secretmanager.secretAccessor"
done

# 3. Create the VM (edit GITHUB_REPO in cloud-startup.sh first)
gcloud compute instances create kalshi-bot \
  --machine-type=e2-micro \
  --zone=us-east1-c \
  --image-family=debian-12 \
  --image-project=debian-cloud \
  --scopes=cloud-platform \
  --metadata-from-file startup-script=cloud-startup.sh
```

On first boot the startup script clones the repo, fetches all secrets, writes `.env`, syncs dependencies, and starts `kalshi-bot.service` via systemd (with `Restart=on-failure`).

**Deploy an update:**
```bash
# SSH into the VM
gcloud compute ssh kalshi-bot --zone=us-east1-c

# Pull latest and restart
sudo git -C /opt/kalshi-btc pull && sudo systemctl restart kalshi-bot
```

**Tail logs:**
```bash
gcloud compute ssh kalshi-bot --zone=us-east1-c
sudo journalctl -u kalshi-bot -f
```

---

## Monitoring

Every 20 seconds the bot logs a status line per active market:

```
Status: KXBTC15M-26APR291445-45  BTC=$75319.02  K=75186.84  sigma=8.3412  tau=271s  p_yes=0.7823  p_no=0.2177  yes_ask=0.7500  no_ask=0.2200
```

Every 5 minutes it logs the current balance:
```
Balance: $24.21
```

Order attempts are logged inline and written in full to `data/trade_log.jsonl`.

---

## Dependencies

- `aiohttp` — async HTTP for REST API and BTC price polling
- `websockets` — Kalshi WebSocket order book stream
- `cryptography` — RSA-PSS request signing
- `python-dotenv` — `.env` loading

Python ≥ 3.12 required. Managed via [uv](https://github.com/astral-sh/uv).
