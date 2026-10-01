# Project State & Multi-Machine Handoff Guide

This file allows you (and any AI assistant on your MacBook or other machines) to immediately resume work on the **Kraken Dual-Engine Micro-Trader** without losing any context.

---

## 1. Quick Resume Prompt for Your MacBook
When you open this project on your work MacBook in Antigravity or any AI assistant, just paste this single prompt:

> *"Please read `HANDOFF.md` and run `python3 src/status.py` to pick up our work on the Kraken dual-engine bot."*

The AI will immediately know the entire system state, architecture, and current live orders.

---

## 2. Infrastructure & Live System State

Both engines run **100% in the AWS cloud (Dublin `eu-west-1`)**, not on your local computer. Your Mac Mini and MacBook are simply remote-control dashboards.

```
Total Portfolio Equity: £78.74 (Unified Kraken Pro Account)
 │
 ├── Engine 1: Volatility Ping-Pong (AWS Lambda, £0.00/mo)
 │     • Allocation: £48.74 across 3 pairs (£15.58/pair)
 │     • Permanent Fee Buffer: £2.00 (Uncommitted cash reserve)
 │     • BTCGBP: Phase 2/2 — Limit Buy filled @ £62,915.60. Limit Sell resting @ £63,909.60 (+£0.17 profit on fill).
 │     • SOLGBP: Phase 1/2 — Limit Buy resting @ £88.62 (0.14% away from filling).
 │     • ETHGBP: Phase 1/2 — Limit Buy resting @ £2,020.50 (0.61% away from filling).
 │
 └── Engine 2: Triangular Arbitrage Daemon (AWS EC2 Dublin, ~£5.20/mo)
       • Instance: i-0b809d2522f8129a0 (t4g.nano in eu-west-1a)
       • Allocation: £30.00 dedicated ring-fenced pool
       • Execution Mode: LIVE (TAKER) with atomic IOC limit orders
       • Net Hurdle: Minimum +0.30% net profit after 3x fees
       • Safeguards: 60s cooldown, instant emergency market exit back to GBP on failure (zero stranded crypto)
```

---

## 3. Machine Setup Checklist (For Your Work MacBook)

To control and inspect the system from your work MacBook, you only need 3 quick steps:

### Step 1: Clone or Pull the Git Repository
```bash
git clone https://github.com/Steven-Robinson/kraken-micro-trader.git
cd kraken-micro-trader
```
*(If already cloned on your MacBook, simply run: `git pull origin main`)*

### Step 2: Configure AWS Credentials
Ensure your work MacBook has AWS CLI access to the same AWS account (`eu-west-1`):
```bash
aws configure
# Default region: eu-west-1
```
*(Verify by running: `aws sts get-caller-identity`)*

### Step 3: Run the Dashboard
```bash
python3 src/status.py
```
This queries the live DynamoDB state, EC2 status in Dublin, and Kraken order books to render your live dual-engine status card.

---

## 4. Key Architecture Files in the Repo

* [`src/status.py`](src/status.py): Unified status dashboard showing both engines, capital ring-fence, and active orders.
* [`src/handler.py`](src/handler.py): AWS Lambda coordinator for the 3-pair volatility ping-pong bot.
* [`src/arbitrage/ws_daemon.py`](src/arbitrage/ws_daemon.py): Real-time WebSocket v2 arbitrage daemon connecting to Kraken matching engines.
* [`src/arbitrage/executor.py`](src/arbitrage/executor.py): Atomic 3-leg execution engine with IOC limit orders and emergency auto-market exits.
* [`terraform/`](terraform/): OpenTofu / Terraform configuration managing VPC, EC2 nano, Lambda, DynamoDB, and IAM roles.
* [`docs/ARBITRAGE_EXPLAINED.md`](docs/ARBITRAGE_EXPLAINED.md): The plain-English Idiot's Guide to triangular arbitrage.
* [`docs/PING_PONG_EXPLAINED.md`](docs/PING_PONG_EXPLAINED.md): The plain-English Idiot's Guide to volatility ping-pong.

---

## 5. Gmail Filter Configuration
* **Label:** `Kraken Trades`
* **Filter Rule:** `subject:("[Kraken Micro-Trader]" OR "[Kraken Arb]" OR "AWS Notification - Subscription Confirmation")`
* **Action:** Skip the Inbox (Archive it) & Apply label `Kraken Trades`.
