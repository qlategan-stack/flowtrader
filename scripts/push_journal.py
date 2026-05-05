"""
Local scheduled task — sync bot journal entries to the dashboard repo
so Streamlit Cloud can display them on the Journal tab.

Runs after each bot session (chained at the end of run_bot.bat).
Deduplicates on `timestamp` so re-runs never create duplicate entries.
Only commits when there is actually something new.

Why local: the bot runs via Windows Task Scheduler on a residential IP.
The journal lives in the bot's working directory and is never visible to
Streamlit Cloud unless we push it to the dashboard repo.
"""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
BOT_ROOT       = Path(__file__).resolve().parent.parent          # trading-bot/trading-bot
DASHBOARD_ROOT = BOT_ROOT.parent.parent / "flowtrader-dashboard" # sibling repo

BOT_JOURNAL    = BOT_ROOT / "journal" / "trades.jsonl"
DASH_JOURNAL   = DASHBOARD_ROOT / "journal" / "trades.jsonl"


GH_USER = "qlategan-stack"


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=DASHBOARD_ROOT, capture_output=True, text=True
    )


def _ensure_gh_user() -> bool:
    """Switch gh CLI to qlategan-stack so git push uses the right credentials."""
    r = subprocess.run(
        ["gh", "auth", "switch", "--user", GH_USER],
        capture_output=True, text=True
    )
    return r.returncode == 0


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def main() -> int:
    if not BOT_JOURNAL.exists():
        print("[journal-push] no bot journal found, skipping")
        return 0

    bot_entries  = _load_jsonl(BOT_JOURNAL)
    dash_entries = _load_jsonl(DASH_JOURNAL)

    existing_ts  = {e.get("timestamp") for e in dash_entries if e.get("timestamp")}
    new_entries  = [e for e in bot_entries if e.get("timestamp") not in existing_ts]

    if not new_entries:
        print(f"[journal-push] no new entries ({len(bot_entries)} in bot journal, all synced)")
        return 0

    DASH_JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with DASH_JOURNAL.open("a", encoding="utf-8") as f:
        for entry in new_entries:
            f.write(json.dumps(entry) + "\n")

    print(f"[journal-push] appended {len(new_entries)} new entr{'y' if len(new_entries) == 1 else 'ies'} to dashboard journal")

    _git("add", "journal/trades.jsonl")
    if _git("diff", "--cached", "--quiet").returncode == 0:
        print("[journal-push] git diff empty after add — nothing to commit")
        return 0

    now_utc = datetime.now(timezone.utc).isoformat()
    msg = f"chore: journal sync {now_utc} (+{len(new_entries)} entries)"
    cm = _git("commit", "-m", msg)
    if cm.returncode != 0:
        print(f"[journal-push] commit failed: {cm.stderr.strip()}")
        return 1

    if not _ensure_gh_user():
        print(f"[journal-push] warning: could not switch gh to {GH_USER}, attempting push anyway")

    push = _git("push")
    if push.returncode != 0:
        print(f"[journal-push] push failed: {push.stderr.strip()}")
        return 1

    print(f"[journal-push] pushed {len(new_entries)} entries to dashboard repo (origin)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
