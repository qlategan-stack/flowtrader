"""
generate_report.py
Pulls live account + market data and recent journal entries,
then writes docs/data.json for the static HTML dashboard.

Run manually:  python generate_report.py
"""

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytz
import yaml
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent))
from data.fetcher import MarketDataFetcher
from journal.logger import JOURNAL_FILE

DOCS_DIR = Path("docs")
DOCS_DIR.mkdir(exist_ok=True)
OUTPUT = DOCS_DIR / "data.json"


def load_journal(days: int = 30) -> list:
    if not JOURNAL_FILE.exists():
        return []
    est = pytz.timezone("America/New_York")
    cutoff = datetime.now(est) - timedelta(days=days)
    entries = []
    with open(JOURNAL_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                ts = datetime.fromisoformat(e["timestamp"])
                if ts.replace(tzinfo=None) > cutoff.replace(tzinfo=None):
                    entries.append(e)
            except Exception:
                continue
    return entries


def performance_stats(entries: list) -> dict:
    trades = [e for e in entries if e.get("action") in ["BUY", "SELL"]
              and e.get("execution_status") == "FILLED"]
    skips  = [e for e in entries if e.get("action") == "SKIP"]
    scores = [e.get("signal_score", 0) for e in entries]
    return {
        "total_cycles":   len(entries),
        "trades_placed":  len(trades),
        "skips":          len(skips),
        "skip_rate":      f"{len(skips)/len(entries)*100:.1f}%" if entries else "0%",
        "avg_signal_score": round(sum(scores)/len(scores), 2) if scores else 0,
        "paper_trades":   sum(1 for t in trades if t.get("paper_trade", True)),
        "live_trades":    sum(1 for t in trades if not t.get("paper_trade", True)),
    }


def main():
    print("Fetching data...")
    fetcher = MarketDataFetcher()

    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    watchlist = cfg.get("watchlist", {}).get("equities", ["SPY", "QQQ"])

    account  = fetcher.get_account_snapshot()
    snapshot = fetcher.build_market_snapshot(watchlist)
    journal  = load_journal(days=30)
    stats    = performance_stats(journal)

    est = pytz.timezone("America/New_York")
    now = datetime.now(est)

    payload = {
        "generated_at": now.isoformat(),
        "generated_at_human": now.strftime("%Y-%m-%d %H:%M:%S EST"),
        "account": account,
        "market": snapshot,
        "journal": journal[-100:],   # last 100 entries
        "stats": stats,
    }

    with open(OUTPUT, "w") as f:
        json.dump(payload, f, indent=2, default=str)

    print(f"Written to {OUTPUT}  ({len(journal)} journal entries, {len(watchlist)} symbols)")


if __name__ == "__main__":
    main()
