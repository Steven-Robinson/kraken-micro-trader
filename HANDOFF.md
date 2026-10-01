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
Total Portfolio Equity: ~£78.26 (Unified Kraken Pro Account)
 │
 ├── Engine 1: Micro-Grid & Ping-Pong Maker (AWS Lambda, £0.00/mo Forever)
 │     • Allocation: £78.26 across 3 pairs (~£25.30/pair)
 │     • Permanent Fee Buffer: £2.35 (Safety reserve)
 │     • ETHGBP: [2-RUNG MICRO-GRID LIVE]
 │         - Rung 1 (50%): BUY_PLACED @ £2,028.14 (-0.60% dip) | Target: +1.00%
 │         - Rung 2 (50%): BUY_PLACED @ £2,011.81 (-1.40% dip) | Target: +1.20%
 │     • BTCGBP: [LEGACY SINGLE-ORDER] Holding BTC @ £62,915.60 | Limit Sell resting @ £63,909.60 (+£0.17 on fill)
 │     • SOLGBP: [LEGACY SINGLE-ORDER] Holding SOL @ £88.62 | Limit Sell resting @ £90.59 (+£0.27 on fill)
 │       (Note: BTC and SOL automatically upgrade to 2-Rung Grid upon sell fill)
 │
 └── Engine 2: Triangular Arbitrage Daemon (EC2 Dublin) [STOPPED / DECOMMISSIONED]
       • Instance: i-0b809d2522f8129a0 (STOPPED — Compute billing halted, saving ~£35/year)
       • Status: All capital safely released 100% to Volatility Micro-Grid
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
To ensure **100% of all bot trade alerts and exchange emails** skip your primary inbox and go directly into your `Kraken Trades` label:

* **Label:** `Kraken Trades`
* **Filter Rule (Recommended — catches 100% of AWS SNS bot alerts & Kraken Pro emails):**
  ```text
  from:(no-reply@sns.amazonaws.com OR noreply@kraken.com)
  ```
* **Alternative Subject Rule:**
  ```text
  subject:("[Kraken Micro-Trader]" OR "[Kraken Arb]" OR "[Kraken Micro-Grid]" OR "AWS Notification - Subscription Confirmation")
  ```
* **Actions Checked:**
  - [x] **Skip the Inbox (Archive it)**
  - [x] **Apply the label:** `Kraken Trades`
  - [x] **Never send it to Spam**

