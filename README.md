# FlowTrader — AI Trading Bot

A Claude-powered mean reversion trading bot built for consistent small daily gains.
Runs automatically via GitHub Actions. Paper trading by default.

---

## How GitHub Actions Works in This Setup

GitHub Actions is essentially a cloud computer that runs your Python code on a schedule.
Here's what happens every 30 minutes during market hours:

```
GitHub Actions server wakes up
    ↓
Pulls your latest code from GitHub
    ↓
Installs Python dependencies
    ↓
Runs main.py → fetches market data → asks Claude → places orders on Alpaca
    ↓
Saves journal entry as an artifact you can download
    ↓
Shuts down (costs you nothing)
```

**Cost:** GitHub Actions is free for public repos, and free for 2,000 minutes/month
on private repos. This bot uses approximately 10–15 minutes per day = ~300 minutes/month.
Well within the free tier.

---

## Setup Instructions (Desktop Required)

### Step 1: Create GitHub Account
Go to github.com and create a free account if you don't have one.

### Step 2: Create a New Repository
1. Click the `+` button → `New repository`
2. Name it: `flowtrader` (or anything you like)
3. Set to **Private** (keeps your strategy secret)
4. Check "Add a README file"
5. Click **Create repository**

### Step 3: Upload the Bot Files
**Option A — GitHub Web Interface (no Git needed):**
1. Open each file from the ZIP you download from Claude
2. In your GitHub repo, click `Add file` → `Upload files`
3. Drag and drop all files, maintaining the folder structure:
   ```
   trading-bot/
   ├── .github/workflows/trading-bot.yml
   ├── agents/decision.py
   ├── agents/executor.py
   ├── data/fetcher.py
   ├── journal/logger.py
   ├── main.py
   ├── config.yaml
   ├── requirements.txt
   ├── CLAUDE.md
   ├── .gitignore
   └── .env.example
   ```
4. Click `Commit changes`

**Option B — Git command line (faster):**
```bash
git clone https://github.com/YOUR_USERNAME/flowtrader.git
cd flowtrader
# Copy all bot files here
git add .
git commit -m "Initial FlowTrader setup"
git push
```

### Step 4: Get Your API Keys

**Alpaca (paper trading — free):**
1. Go to alpaca.markets → Sign up (it's free)
2. Dashboard → Paper Trading → API Keys → Generate New Key
3. Copy the API Key ID and Secret Key

**Anthropic (Claude API):**
1. Go to console.anthropic.com
2. API Keys → Create Key
3. Copy the key (starts with `sk-ant-`)

**Alpha Vantage (free tier, optional but recommended):**
1. Go to alphavantage.co → Get Free API Key
2. Takes 30 seconds, no credit card needed

### Step 5: Add Secrets to GitHub
This is where your API keys live — safely encrypted, never in your code.

1. In your GitHub repo: **Settings** → **Secrets and variables** → **Actions**
2. Click **New repository secret** for each:

| Secret Name | Value |
|---|---|
| `ALPACA_API_KEY` | Your Alpaca API Key ID |
| `ALPACA_SECRET_KEY` | Your Alpaca Secret Key |
| `ANTHROPIC_API_KEY` | Your Claude API key |
| `ALPHA_VANTAGE_KEY` | Your Alpha Vantage key |
| `PAPER_TRADING` | `true` (leave as true until you're confident) |
| `SLACK_WEBHOOK_URL` | Optional — your Slack webhook for notifications |

### Step 6: Enable GitHub Actions
1. In your repo, click the **Actions** tab
2. Click **Enable Actions** if prompted
3. You should see "FlowTrader — Trading Bot" in the workflow list

### Step 7: Run a Test
1. Click on the workflow → **Run workflow** → Select `test` mode → **Run workflow**
2. Watch the logs — it should connect to Alpaca and fetch your account balance
3. If it prints your account value, everything is working

---

## How to Monitor the Bot

### View Run Logs
- GitHub repo → **Actions** tab
- Click on any run to see the full log output
- Each run shows what Claude decided and why

### Download Journal Entries
- Click a completed run → **Artifacts** section
- Download `trading-journal-XXXXX`
- Open `trades.jsonl` — one JSON object per line, every decision logged

### Weekly Review
- Every Sunday the bot automatically runs in `weekly-review` mode
- Claude reads the past week's journal and writes a performance analysis
- Download the artifact to see `weekly_summary.md`

---

## Configuration

Edit `config.yaml` to customize:
- **Watchlist**: Add or remove stocks/crypto symbols
- **Risk settings**: Position size, daily loss limit, max positions
- **Strategy**: RSI thresholds, Bollinger Band settings, ADX filter

---

## Safety Checklist Before Going Live

- [ ] Bot has been running on paper for at least 30 days
- [ ] Win rate is above 50% over at least 30 trades
- [ ] You have read every weekly summary
- [ ] You understand why each losing trade lost
- [ ] Daily loss limit is set and tested
- [ ] Live account is funded with money you can afford to lose
- [ ] Change `PAPER_TRADING` secret to `false` in GitHub Secrets
- [ ] Set `LIVE_TRADING_CONFIRMED` to `true` in GitHub Secrets

**Start with your smallest comfortable amount. The bot doesn't need much capital to prove itself.**

---

## File Structure

```
flowtrader/
├── .github/
│   └── workflows/
│       └── trading-bot.yml    ← GitHub Actions schedule and runner
├── agents/
│   ├── decision.py            ← Claude decision engine
│   └── executor.py            ← Order execution + guardrails
├── data/
│   └── fetcher.py             ← Market data, news, indicators
├── journal/
│   └── logger.py              ← Trade journal + weekly review
├── main.py                    ← Entry point — orchestrates everything
├── config.yaml                ← Watchlist and strategy settings
├── CLAUDE.md                  ← Claude's system prompt and hard rules
├── requirements.txt           ← Python dependencies
└── .env.example               ← API key template (copy to .env locally)
```

---

## Important: This is Paper Trading by Default

The bot will NOT place real trades until you:
1. Have tested on paper for at minimum 30 days
2. Set `PAPER_TRADING=false` in GitHub Secrets
3. Set `LIVE_TRADING_CONFIRMED=true` in GitHub Secrets

Both flags must be changed. This double-lock prevents accidents.

---

## Disclaimer

This bot is for educational and research purposes. It is not financial advice.
All trading involves risk. Past performance does not guarantee future results.
Never trade with money you cannot afford to lose. Always paper trade first.

*Built with Claude by Flowmatic Automation*
