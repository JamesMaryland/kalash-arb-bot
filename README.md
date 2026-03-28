# Kalshi Latency Arbitrage Bot

A Python 3.11+ bot that exploits pricing lag between Kalshi's BTC/ETH binary contracts and real-time Binance prices. Runs in paper trading mode by default — three explicit flags are required to enable live trading.

---

## Features

- **Latency arbitrage engine** — detects when Kalshi's implied probability lags Binance spot prices by more than 3 percentage points
- **Real-time Binance feed** — WebSocket `bookTicker` stream for BTC and ETH with auto-reconnect and exponential back-off
- **Kalshi CLOB integration** — monitors BTC/ETH 5-minute and 15-minute up/down contracts via `py-clob-client`
- **Multi-factor confidence scoring** — momentum strength, volatility, edge magnitude, spread quality, and volume; only trades when confidence > 85%
- **Half-Kelly position sizing** — fractional Kelly Criterion with a hard cap of 8% of portfolio per position
- **Paper trading by default** — all three flags (`--enable-live-trading`, `--confirm-live`, `--override-live`) must be explicitly set to go live
- **Kill switch** — halts all trading if daily drawdown exceeds 20%; resets at midnight
- **Telegram alerts** — notifications on every trade open/close, drawdown thresholds (10%, 15%, 20%), kill switch activation, and daily summary
- **SQLite persistence** — full trade history, open positions, price snapshots, and daily P&L stats
- **Rich terminal dashboard** — live P&L, win rate, open positions, real-time prices with momentum arrows, last 10 trades

---

## Requirements

- Python 3.11 or later
- A [Kalshi](https://kalshi.com) account with API access (demo or prod)
- A [Telegram bot token](https://core.telegram.org/bots#botfather) and chat ID
- Internet access to `wss://stream.binance.com:9443`

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/JamesMaryland/kalash-arb-bot.git
cd kalash-arb-bot

# 2. Create and activate a virtual environment
python3.11 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
```

---

## Configuration

Edit `.env` with your credentials and risk parameters:

```ini
# Kalshi API
KALSHI_API_KEY=your_api_key_here
KALSHI_API_SECRET=your_api_secret_here
KALSHI_API_ENV=demo              # demo or prod

# Telegram
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here

# Portfolio
PORTFOLIO_BALANCE=10000.00       # Starting paper-trade balance (USD)

# Risk parameters
MAX_POSITION_PCT=0.08            # Max 8% of portfolio per position
KELLY_FRACTION=0.5               # Half-Kelly
MIN_EDGE_PCT=0.05                # Minimum 5% edge to enter
MIN_CONFIDENCE=0.85              # Minimum 85% confidence score
LAG_THRESHOLD_PCT=0.03           # Kalshi must lag CEX by >3pp to flag

# Kill switch
DAILY_DRAWDOWN_LIMIT=0.20        # Halt trading if daily drawdown > 20%

# Live trading — all three must be "true" to leave paper mode
LIVE_TRADING_ENABLE=false
LIVE_TRADING_CONFIRM=false
LIVE_TRADING_OVERRIDE=false
```

### Getting a Telegram bot token

1. Open Telegram and search for `@BotFather`
2. Send `/newbot` and follow the prompts
3. Copy the token into `TELEGRAM_BOT_TOKEN`
4. Send any message to your bot, then visit:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
5. Copy the `chat.id` value into `TELEGRAM_CHAT_ID`

### Getting Kalshi API credentials

1. Log in to [Kalshi](https://kalshi.com) (or [demo](https://demo.kalshi.co))
2. Go to **Settings → API**
3. Generate a new key pair and copy both values into `.env`
4. Set `KALSHI_API_ENV=demo` while testing, `prod` for real money

---

## Usage

### Paper trading (default — safe)

```bash
python main.py
```

or explicitly:

```bash
python main.py --paper
```

### Live trading

All three flags must be passed simultaneously. Omitting even one keeps the bot in paper mode:

```bash
python main.py \
  --enable-live-trading \
  --confirm-live \
  --override-live
```

A 5-second countdown is displayed before live trading begins so you can abort with `Ctrl-C`.

### CLI options

```
usage: main.py [-h] [--paper] [--enable-live-trading] [--confirm-live]
               [--override-live] [--log-level {DEBUG,INFO,WARNING,ERROR}]

options:
  --paper                Force paper trading mode (default)
  --enable-live-trading  Live trading flag 1/3
  --confirm-live         Live trading flag 2/3
  --override-live        Live trading flag 3/3
  --log-level            Logging verbosity (default: INFO)
```

### Running 24/7 with tmux (Linux / DigitalOcean)

`tmux` lets the bot keep running after you close your SSH session or terminal.
The Rich dashboard renders correctly inside a tmux session.

**Install tmux**
```bash
sudo apt install tmux -y   # Ubuntu/Debian
```

**Start a session and run the bot**
```bash
tmux new -s arb-bot
source .venv/bin/activate
python main.py
```

**Detach — leave the bot running, return to your shell**
```
Ctrl+B  then  D
```

**Reattach from any SSH session**
```bash
tmux attach -t arb-bot
```

**Other useful commands**
```bash
tmux ls                        # list active sessions
tmux kill-session -t arb-bot   # stop the bot and close the session
```

> **Note:** tmux is not available natively on Windows. It is only needed when
> hosting on a Linux server (e.g. DigitalOcean). On Windows, simply leave the
> Command Prompt window open.

---

## Terminal Dashboard

Once running, the terminal shows a live-updating layout:

```
┌─────────────────── KALSHI LATENCY ARB BOT  [PAPER TRADING]  Uptime: 00:12:34 ───┐
│ Portfolio        │ Open Positions                    │ Prices                    │
│ Balance $9,984   │ ID  Market         Side  Entry    │ BTC  $83,201  ▲ +0.0021%  │
│ Equity  $9,986   │ 3   KXBTC-5M-...  YES   0.4800   │ ETH  $1,821   ▼ -0.0008%  │
│ Day P&L  +$2.10  │ ...                               │ Mkts 4/8 live/total       │
│ Win Rate  61.5%  │                                   │                           │
├──────────────────┴───────────────────────────────────┴───────────────────────────┤
│ Last 10 Trades                                                                    │
│ ID  Market           Asset  Dur   Side  Entry   Exit   P&L      Conf   Edge      │
│  3  KXBTC-5M-T...    BTC    5M_UP  YES  0.4800  —      open     91%    7.2%      │
│  2  KXETH-15M-T...   ETH    15M_D  NO   0.5100  1.000  +0.4900  88%    6.1%      │
└───────────────────────────────────────────────────────────────────────────────────┘
```

---

## How the Arbitrage Logic Works

### 1. Fair value estimation

Every 30 seconds of rolling BTC/ETH price history from Binance is used to compute:

- **Momentum** — fractional price change over the window
- **Volatility** — standard deviation of log-returns
- **Fair probability** — `0.50 + clamp(momentum / (4 × vol), -0.30, +0.30)`

### 2. Opportunity detection

For each live Kalshi contract:

```
delta = fair_prob_yes - kalshi_implied_prob
```

If `|delta| > 3pp` (configurable), the contract is a candidate. The side we trade:
- `delta > 0` → Kalshi is underpricing YES → buy YES
- `delta < 0` → Kalshi is underpricing NO → buy NO

### 3. Entry gates (all must pass)

| Gate | Default |
|------|---------|
| Edge > minimum | > 5% |
| Confidence score | > 85% |
| Position size | < 8% of portfolio |
| Kill switch | inactive |
| Dedup window | no open position in same market, 30s cooldown |

### 4. Position sizing (half-Kelly)

```
b  = (1 - entry_price) / entry_price   # net odds on a win
f* = p - q/b                           # full Kelly fraction
size = portfolio_value × f* × 0.5      # half-Kelly, capped at 8%
```

### 5. Settlement

Contracts are monitored until they expire. When a market disappears from the live quote feed the bot queries the Kalshi API for the final result (`yes` / `no`) and books the P&L accordingly.

---

## Project Structure

```
kalash-arb-bot/
├── main.py                  # Entry point, asyncio orchestration, CLI
├── requirements.txt
├── .env.example             # Config template
└── bot/
    ├── config.py            # Typed config dataclasses, loaded from .env
    ├── database.py          # Async SQLite schema and queries
    ├── binance_feed.py      # Binance WebSocket price feed
    ├── kalshi_client.py     # Kalshi CLOB API wrapper + rate limiter
    ├── arb_engine.py        # Signal detection, confidence scoring, Kelly sizing
    ├── position_manager.py  # Portfolio state, drawdown tracking, kill switch
    ├── executor.py          # Order submission, dedup, risk gates
    ├── telegram_notifier.py # Async Telegram alert queue
    └── dashboard.py         # Rich terminal UI
```

---

## Risk Warnings

- **Binary contracts expire worthless** if the price moves against you. Even high-confidence trades lose.
- **Latency arbitrage is competitive.** Other bots may fill the same edge before your order lands.
- **Kalshi spreads can be wide** on low-volume markets. The bot skips markets with spreads > 10 cents.
- The kill switch protects against catastrophic drawdown but does not guarantee capital preservation.
- Always run in paper mode first and review the logs before enabling live trading.

---

## License

MIT
