#  Polymarket BTC Reversion Bot (15min)

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![Platform](https://img.shields.io/badge/Platform-Polymarket-green)
![Strategy](https://img.shields.io/badge/Strategy-Mean%20Reversion-orange)

This bot automatically trades on **Polymarket's "Bitcoin Up/Down" markets (15-minute candles)** using the **CLOB (Central Limit Order Book) API**.

It implements a **mean reversion strategy** based on statistical analysis of consecutive candle trends, executing Limit Orders automatically and sending real-time notifications via Telegram.

---

##  The Strategy

The core logic relies on the conditional probability of a trend reversal following a short, sustained movement in 15-minute intervals.

### 1. Statistical Premise
Based on historical analysis of Bitcoin 15-minute candles, it has been determined that after **2 consecutive candles of the same color** (e.g., `UP` + `UP` or `DOWN` + `DOWN`), there is a **54.8% probability** that the next candle will be the opposite color.

* **Signal:** The 2 previous candles closed with the same result.
* **Action:** Bet *against* the trend for the next candle (e.g., if 2 UPs occurred, bet DOWN).
* **Execution:** The bot wakes up during the final minutes of the current candle (configurable) and places a **GTC (Good Till Cancelled)** Limit Order.

### 2. Expected Value (EV) & ROI
The bot is configured to buy shares at a maximum price of **$0.51**. Even with a small statistical edge, this price cap ensures a positive Expected Value.

The formula for Expected Value ($EV$) per trade is:

$$EV = (P_{win} \times \text{Net Profit}) - (P_{loss} \times \text{Cost})$$

Assuming a purchase at **$0.51** (worst-case limit) looking for a **$1.00** payout:

* **Win Probability ($P_{win}$):** $0.548$ (54.8%)
* **Loss Probability ($P_{loss}$):** $0.452$ (45.2%)
* **Net Profit:** $0.49$ ($1.00 payout - $0.51 cost)
* **Cost (Risk):** $0.51$

$$EV = (0.548 \times 0.49) - (0.452 \times 0.51)$$
$$EV = 0.26852 - 0.23052$$
$$EV \approx \$0.038 \text{ per share}$$

#### Return on Investment (ROI)
To calculate the edge (profitability) on the capital risked:

$$ROI = \frac{EV}{Cost} \times 100$$
$$ROI = \frac{0.038}{0.51} \times 100 \approx \mathbf{7.45\%}$$

**Theoretical conclusion:** Every trade executed at the limit price would have a mathematical edge of **~7.45%** over the market in the long run.

---

#### Realistic Expected Value (with 10% false entries)

In practice, the **real Expected Value is lower** than the theoretical one.

Due to how the code detects signals, we assume that in **10% of the cases** the bot will trigger a **false entry**. That is because to estimate whether the current candle will close up or down, we treat the Polymarket price as the implied probability of each outcome. Our trigger is in 90c, meaning that 90% of the times we will buy when it is supposed to. But in those other 10% false entries:

* The bot still buys at **$0.51**.
* We assume the outcome behaves like a **fair coin**:  
  $$P_{win}^{false} = 0.50,\quad P_{loss}^{false} = 0.50$$

For these noisy trades, the EV becomes:

* **Net Profit:** $0.49$
* **Cost:** $0.51$

$$EV_{false} = (0.50 \times 0.49) - (0.50 \times 0.51)$$
$$EV_{false} = 0.245 - 0.255 = -0.01$$

We now combine both scenarios:

* **90% of trades** follow the historical edge ($EV_{clean} = +0.038$).
* **10% of trades** are false entries ($EV_{false} = -0.01$).

The **real Expected Value per share** is:

$$EV_{real} = 0.9 \times 0.038 + 0.1 \times (-0.01)$$
$$EV_{real} = 0.0342 - 0.001 = 0.0332$$

So the **realistic EV** is approximately:

$$EV_{real} \approx \$0.0332 \text{ per share}$$

And the corresponding **realistic ROI** is:

$$ROI_{real} = \frac{EV_{real}}{Cost} \times 100 = \frac{0.0332}{0.51} \times 100 \approx \mathbf{6.5\%}$$

**Real-world conclusion:** After discounting a 10% rate of false entries, the edge drops from **7.45%** to roughly **6.5%**, but it remains clearly positive.

So why not use a higher target to have less false entries? We deliberately use this conservative target because:

* There are **other traders running similar (or same) mean reversion strategies** on the same Polymarket markets.
* **Liquidity is limited**, so we want our order to be **first in the book** to get filled.
* Being first sometimes means accepting a **smaller theoretical margin**, but this trade-off is compensated by a higher fill probability and still maintaining a positive EV.

---

##  Key Features

* **⚡ Direct CLOB Integration:** Uses `py_clob_client` to trade directly on the Layer 2 Polygon order book for maximum speed and minimal fees.
* **🛡️ Smart Order Management:**
    * Places **Limit GTC** orders with a hard price cap (`max_price` of 0.51).
    * Automatically cancels expired orders that didn't fill before the candle closed.
* **📊 Market Resolution:** Checks both the Gamma API and live underlying prices to accurately determine the outcome of previous candles before trading.
* **🔔 Telegram Alerts:** Fully integrated notification system for:
    * Signal detection (UP/DOWN).
    * Order placement confirmation (with Order ID).
    * Error logging (API or Network issues).

---

##  Requirements

* **Python 3.10+**
* **Polymarket Account:** Must have funds deposited on the Polygon network (USDC.e).
* **API Keys:** You need your Polymarket Proxy Wallet (L2) Private Key.
* **Telegram Bot:** For receiving alerts.

### Dependencies

Install the required Python libraries:

```bash
pip install py-clob-client requests python-dotenv
