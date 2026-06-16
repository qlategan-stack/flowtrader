"""
scripts/heartbeat_watchdog.py — independent bot liveness watchdog (C-2, audit 2026-06-16).

The June 10–13 blackout went unnoticed for 4 days because nothing watches the
watcher: when the bot stops running, the bot can't alert about itself. This
script runs on its OWN Windows Task Scheduler entry (every 30–60 min) and is
the thing that notices the bot has gone silent.

It reads the most recent trades.jsonl timestamp. If the newest entry is older
than --max-silence-mins (default 90 = three missed 30-min cycles), it fires a
Telegram alert. State is kept in journal/last_heartbeat_alert.json so it alerts
once per outage, not every run (re-alerts every --realert-hours, default 12).

Independent of the bot's own API health: it does NOT call Claude or any exchange,
only reads a local file and POSTs to Telegram, so it keeps working even when the
outage is an API/credit failure (the exact June 9 → June 15 failure modes).

Usage:
    python scripts/heartbeat_watchdog.py
    python scripts/heartbeat_watchdog.py --max-silence-mins 120
    python scripts/heartbeat_watchdog.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

_ROOT = Path(__file__).resolve().parent.parent
_JOURNAL = _ROOT / "journal" / "trades.jsonl"
_STATE = _ROOT / "journal" / "last_heartbeat_alert.json"


def _newest_entry_dt() -> datetime | None:
    if not _JOURNAL.exists():
        return None
    newest = None
    for line in _JOURNAL.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ts = json.loads(line).get("timestamp")
        except json.JSONDecodeError:
            continue
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if newest is None or dt > newest:
            newest = dt
    return newest


def _send_telegram(text: str, retries: int = 3) -> bool:
    """POST to Telegram with DNS-failure retries (M-6: getaddrinfo failed was
    transient on 2026-06-16). Returns True on a 200."""
    import time
    import requests
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[watchdog] no Telegram token/chat configured — cannot alert")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for attempt in range(1, retries + 1):
        try:
            socket.gethostbyname("api.telegram.org")  # surface DNS failure fast
            r = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
            if r.status_code == 200:
                return True
            print(f"[watchdog] telegram HTTP {r.status_code}: {r.text[:120]}")
        except Exception as e:
            print(f"[watchdog] telegram attempt {attempt}/{retries} failed: {e}")
            time.sleep(2 * attempt)  # backoff, helps transient DNS
    return False


def _load_state() -> dict:
    try:
        return json.loads(_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        _STATE.parent.mkdir(parents=True, exist_ok=True)
        _STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[watchdog] could not persist state: {e}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--max-silence-mins", type=int, default=90)
    p.add_argument("--realert-hours", type=float, default=12.0)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    now = datetime.now(timezone.utc)
    newest = _newest_entry_dt()
    if newest is None:
        print("[watchdog] no journal entries found — cannot assess liveness")
        return 0

    silence = now - newest
    silence_mins = silence.total_seconds() / 60
    print(f"[watchdog] newest journal entry {newest.isoformat()} "
          f"({silence_mins:.0f} min ago; threshold {args.max_silence_mins})")

    if silence_mins <= args.max_silence_mins:
        return 0  # bot is alive

    # Bot silent beyond threshold — alert (rate-limited).
    state = _load_state()
    last_alert = state.get("last_alert_utc")
    if last_alert:
        try:
            if (now - datetime.fromisoformat(last_alert)) < timedelta(hours=args.realert_hours):
                print("[watchdog] already alerted recently — suppressing re-alert")
                return 0
        except ValueError:
            pass

    msg = (
        f"🔴 FlowTrader watchdog — bot appears DOWN.\n"
        f"No journal activity for {silence_mins/60:.1f}h "
        f"(last entry {newest.isoformat()}).\n"
        f"Check Task Scheduler (run_bot.bat), ANTHROPIC_API_KEY, and credits."
    )
    # ASCII-safe console line (Windows cp1252 console can't encode the emoji;
    # the Telegram message itself is sent as UTF-8 over HTTP and keeps it).
    print("[watchdog] " + msg.replace("\n", " | ").encode("ascii", "replace").decode("ascii"))
    if args.dry_run:
        print("[watchdog] --dry-run: no alert sent")
        return 0
    if _send_telegram(msg):
        _save_state({"last_alert_utc": now.isoformat(), "silent_since": newest.isoformat()})
        print("[watchdog] alert sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
