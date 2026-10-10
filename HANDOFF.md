# Developer Setup & Multi-Machine Handoff Guide

This file provides instructions for setting up and resuming development on the **Kraken Autonomous Micro-Grid Trader** across developer workstations.

---

## 1. Quick Resume Prompt for AI Assistant
When resuming development in an AI coding assistant:

> *"Please read `HANDOFF.md` and run `python3 src/status.py` to pick up our work on the Kraken micro-grid bot."*

---

## 2. Infrastructure Architecture

The bot runs **100% serverlessly in AWS (`eu-west-1`)**. Local workstations function as remote-control dashboards and testing environments.

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

## 3. Workstation Setup Checklist

To control and inspect the system locally, follow these 3 steps:

### Step 1: Clone or Pull the Git Repository
```bash
git clone https://github.com/Steven-Robinson/kraken-micro-trader.git
cd kraken-micro-trader
```

### Step 2: Configure AWS Credentials
Ensure your workstation has AWS CLI access to your AWS account (`eu-west-1`):
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
