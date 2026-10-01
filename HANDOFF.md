# Project State & Multi-Machine Handoff Guide

This file allows you (and any AI assistant on your MacBook or other machines) to immediately resume work on the **Kraken Autonomous Micro-Grid Trader** without losing any context.

---

## 1. Quick Resume Prompt for Your MacBook
When you open this project on your work MacBook in Antigravity or any AI assistant, just paste this single prompt:

> *"Please read `HANDOFF.md` and run `python3 src/status.py` to pick up our work on the Kraken micro-grid bot."*

The AI will immediately know the entire system state, architecture, and current live orders.

---

## 2. Infrastructure & Live System State

The bot runs **100% serverless in AWS Dublin (`eu-west-1`)**, not on your local computer. Your Mac Mini and MacBook are simply remote-control dashboards.

```
Total Portfolio Equity: ~£78.51 (Free Cash: £19.79 | Unified Kraken Pro Account)
 │
 └── Strategy: 2-Rung Micro-Grid Maker (AWS Lambda, £0.00/mo Forever)
       • Allocation: £78.51 across 3 pairs (~£25.39/pair)
       • Permanent Fee Buffer: £2.36 (3% safety reserve floor)
       • Realized Profit To Date: +£0.1675 (1 cycle completed)
       • BTCGBP: [2-RUNG MICRO-GRID LIVE] (Upgraded after £63,909.60 sell fill!)
           - Rung 1 (50%): BUY_PLACED @ £63,509.3 (-0.60% dip) | Target: +1.00%
           - Rung 2 (50%): IDLE (Queued to deploy buy order)
       • ETHGBP: [2-RUNG MICRO-GRID LIVE]
           - Rung 1 (50%): SELL_PLACED @ £2,058.56 (Holding 0.00623724 ETH, +£0.13 profit on fill)
           - Rung 2 (50%): BUY_PLACED @ £2,011.81 (-1.40% dip) | Target: +1.20%
       • SOLGBP: [LEGACY SINGLE-ORDER] Holding SOL @ £88.62 | Limit Sell resting @ £90.59 (+£0.27 on fill)
         (Note: SOL automatically upgrades to 2-Rung Grid upon sell fill)
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
This queries the live DynamoDB state and Kraken order books to render your live micro-grid status card.

---

## 4. Key Architecture Files in the Repo

* [`src/status.py`](src/status.py): Terminal status dashboard showing portfolio equity, fee buffer, and per-pair grid states.
* [`src/handler.py`](src/handler.py): AWS Lambda coordinator executing the 2-minute cycle, deposit detection, and order management.
* [`src/grid_engine.py`](src/grid_engine.py): 2-Rung Micro-Grid state machine engine managing independent limit order pairs.
* [`src/volatility.py`](src/volatility.py): ATR-based dynamic volatility engine computing adaptive profit targets.
* [`src/kraken_client.py`](src/kraken_client.py): Authenticated HMAC-SHA512 REST API client for Kraken Pro.
* [`terraform/`](terraform/): OpenTofu / Terraform configuration managing Lambda, DynamoDB, EventBridge, and IAM.
* [`docs/PING_PONG_EXPLAINED.md`](docs/PING_PONG_EXPLAINED.md): Plain-English Idiot's Guide to volatility ping-pong and micro-grids.

---

## 5. Gmail Filter Configuration
To ensure **100% of all bot trade alerts and exchange emails** skip your primary inbox and go directly into your `Kraken Trades` label:

* **Label:** `Kraken Trades`
* **Filter Rule (Catches 100% of AWS SNS bot alerts & Kraken Pro emails):**
  ```text
  from:(no-reply@sns.amazonaws.com OR noreply@kraken.com)
  ```
* **Actions Checked:**
  - [x] **Skip the Inbox (Archive it)**
  - [x] **Apply the label:** `Kraken Trades`
  - [x] **Never send it to Spam**
