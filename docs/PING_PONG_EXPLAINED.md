# The Plain-English "Idiot's Guide" to Volatility Ping-Pong
*(Or: How your bot buys low, sells slightly higher, and prints small cash profits all day long)*

---

## 1. What is "Ping-Pong" Trading?

Imagine a tennis table where the ball bounces back and forth between two paddles:

```
    [ Upper Paddle: SELL at £91.75 ]  ➔ Locks in +£0.72 Profit
             ▲                 │
             │ (Market rises)  │ (Bot waits for dip)
             │                 ▼
    [ Lower Paddle: BUY at £89.95 ]   ➔ Scoops up cheap coins
```

* **Step 1 (Paddle Low):** The bot places an order to buy a coin at a discount.
* **Step 2 (The Fill):** The market dips, someone sells to us, and we now own the coin.
* **Step 3 (Paddle High):** The bot immediately calculates a target profit price (e.g. +1.5%) and places an order to sell.
* **Step 4 (The Profit):** The market ticks up, someone buys our coins at the higher price, and our cash returns with a profit!
* **Step 5 (Repeat):** The bot puts the lower paddle back in the water and waits for the next dip.

**Ping. Pong. Repeat.**

---

## 2. Why Maker-Only Limit Orders? (The Fee Secret)

When you trade on Kraken Pro, there are two ways to place an order:

1. **Taker Order (Market Order):**
   * *"I want to buy right now at whatever price is on the screen!"*
   * Kraken charges you a higher fee: **0.40%**.
   * You also suffer "slippage" (you might pay slightly more than you thought).

2. **Maker Order (Post-Only Limit Order):**
   * *"I am putting an offer on the book at £89.95. I will wait for someone else to come to me."*
   * Kraken rewards you with a lower fee: **0.25%**.
   * You get **exact pricing**—never a penny worse.

Our bot **only uses Maker orders** (`oflags = post`).
* When buying, we post our order and wait.
* When selling, we post our order and wait.
* This cuts our fee overhead almost in half and ensures every trade executes at the exact price we intended.

---

## 3. How Does the Bot Know Where to Sell? (The Dynamic Volatility Formula)

Amateur trading bots use rigid targets:
> *"Always sell at +5.0% profit."*

In cryptocurrency, if the market only moves 2% today, a +5% sell order will sit there stranded for weeks or months doing nothing.

Our bot uses a **Dynamic Half-Spread Volatility Engine**:
1. It looks at the last 24 hours of hourly price candles on Kraken.
2. It measures the high and low of the day:
   $$\text{Daily Spread} = \frac{\text{24h High} - \text{24h Low}}{\text{24h Low}}$$
   *(For example, if Solana went between £88.00 and £92.00, the spread was ~4.5%).*
3. It takes **50% of that spread** (the typical oscillation from the midpoint to an extreme):
   $$4.5\% \times 0.50 = 2.25\%$$
4. It aims to capture **75% of that half-spread**:
   $$2.25\% \times 0.75 = \mathbf{+1.68\% \text{ Net Profit Target}}$$

### Why this works brilliantly:
* When the market is quiet, the target shrinks (e.g. down to `+0.9%`), allowing the bot to complete quick, frequent trades.
* When the market is volatile, the target widens (up to `+3.0%`), capturing bigger profits on big swings.
* We never ask the market to break 24-hour records—we just capture the normal daily waves.

---

## 4. What Happens When a Coin Drops After Buying?

This is the most common question:
> *"What if the bot buys Solana at £89.00, and the price drops to £85.00?"*

Here is how our bot handles it:
1. **No Panic Selling:** The bot **never** panic-sells at a loss. It holds the coins securely in your Kraken account.
2. **We Only Trade High-Conviction "Blue Chips":** We don't trade obscure meme coins that go to zero. We trade **Bitcoin (`BTC`), Ethereum (`ETH`), and Solana (`SOL`)**—the top three most liquid cryptocurrencies in the world.
3. **Patience is Profit:** Cryptocurrencies naturally breathe in waves. When the market recovers (which blue chips do repeatedly), our resting sell order at £90.50 gets hit, and we walk away with our original profit.
4. **Independent Pair Isolation:** Even if Solana is currently waiting for a bounce, the Ethereum and Bitcoin bots continue ping-ponging independently. One pair never locks up the entire portfolio.

---

## 5. Multi-Pair Concurrent Scaling

The bot manages three pairs simultaneously:
1. **`SOLGBP`:** Highly volatile, fast 3-5% daily swings, frequent cycle completions.
2. **`ETHGBP`:** Medium volatility, steady institutional volume.
3. **`BTCGBP`:** The king of crypto, lowest volatility, most reliable long-term bounce.

### How the money is divided:
If your Ping-Pong trading equity is **£48.78**:
* We set aside a permanent **£2.00 fee buffer** in cash:
  $$\text{Tradeable Cash} = £48.78 - £2.00 = £46.78$$
* We divide that equally across the 3 pairs:
  $$\frac{£46.78}{3} = \mathbf{£15.59 \text{ per pair}}$$

Each pair operates on its own dedicated £15.59 allocation.

---

## 6. Real Live Milestone: Bitcoin Cycle Walkthrough

Here is the exact sequence of events that just happened on our live system:

```text
[Step 1: The Buy Order]
  • Market was hovering around £63,000.
  • Bot posted a Maker limit buy for 0.00024747 BTC @ £62,915.60.

[Step 2: The Fill]
  • Bitcoin took a quick dip, and someone sold to us at £62,915.60!
  • Total spent: £15.57.

[Step 3: Calculating Profit]
  • Bot checked 24h volatility (2.65% spread).
  • Dynamic target set to +0.99% net.
  • With 0.50% round-trip fee allowance, target sell price = £63,909.60.

[Step 4: The Resting Sell Order]
  • Bot immediately posted Maker sell order O4MFXV-W3O5W-YFEZQ3 @ £63,909.60.
  • The bot is now waiting patiently on Kraken's order book.

[Step 5: When It Fills]
  • When Bitcoin touches £63,909.60, the sell executes automatically.
  • Cash returned: £15.81 (an instant +£0.17 clean profit).
  • The bot will immediately queue a new buy order at a lower price.
```

---

## 7. How Deposits Are Automatically Detected

Whenever you transfer extra money from your bank (e.g. £50 top-up):
* The bot wakes up every 2 minutes.
* It compares your total account equity and cash against the previous run.
* If **both** equity and cash increase by $\ge £5.00$, it recognizes an external bank transfer!
* It immediately sends you an email:
  ```text
  Subject: [Kraken Micro-Trader] New Deposit Detected! (+£50.00 Added to Trading)
  ```
* It recalculates your per-pair trading size and automatically scales up your future orders.

---

## 8. Why This System Costs £0.00 to Run

Most trading bots require a server running 24/7.
This bot uses **AWS Serverless Architecture**:
* **AWS Lambda:** The bot only runs for about 1.5 seconds every 2 minutes. The rest of the time, Kraken's matching engine does all the work for free.
* **Amazon EventBridge:** The 2-minute timer is completely free.
* **Amazon DynamoDB:** Stores order IDs and profit tracking in the AWS Free Tier.
* **Amazon SNS:** Free email alerts.

**Total Cloud Bill: £0.00 / month.** Every single penny of trading profit stays in your pocket.
