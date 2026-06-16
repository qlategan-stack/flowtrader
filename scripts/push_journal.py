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

SUGGESTION_FILES = ["suggestions_in.jsonl", "suggestions_out.jsonl"]


GH_USER = "qlategan-stack"


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=DASHBOARD_ROOT, capture_output=True, text=True
    )


def _ensure_repo_healthy() -> bool:
    """Detect and auto-repair a broken refs/heads/main (H-2 audit 2026-06-16).

    A failed/interrupted push can leave refs/heads/main pointing at an invalid
    object ("cannot lock ref 'HEAD': … reference broken"), which silently fails
    every subsequent commit until repaired by hand. This detects that state and
    re-points main at origin/main (fetched), the same manual repair done on
    2026-06-16. Returns True if the repo is healthy (or was repaired), False if
    it could not be fixed (caller should skip the push and log loudly).
    """
    if _git("rev-parse", "--verify", "HEAD").returncode == 0:
        return True  # HEAD resolves — healthy
    print("[journal-push] WARNING: HEAD/main ref appears broken — attempting auto-repair")
    _git("fetch", "origin")
    # Re-point main at the fetched origin/main.
    origin = _git("rev-parse", "--verify", "origin/main")
    if origin.returncode != 0:
        print("[journal-push] auto-repair FAILED: cannot resolve origin/main")
        return False
    sha = origin.stdout.strip()
    fixed = _git("update-ref", "refs/heads/main", sha)
    if fixed.returncode != 0:
        # update-ref can itself fail to lock a zeroed ref; write it directly.
        try:
            (DASHBOARD_ROOT / ".git" / "refs" / "heads" / "main").write_text(sha + "\n", encoding="utf-8")
        except Exception as e:
            print(f"[journal-push] auto-repair FAILED: {e}")
            return False
    ok = _git("rev-parse", "--verify", "HEAD").returncode == 0
    print(f"[journal-push] auto-repair {'succeeded' if ok else 'FAILED'} (main -> {sha[:8]})")
    return ok


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
    # utf-8-sig tolerates an accidental BOM at the start of the file (earlier
    # writes from a notebook or PowerShell pipe left BOMs in trades.jsonl, which
    # made json.loads fail on the first line).
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _sync_suggestions() -> list[str]:
    """Copy suggestions_*.jsonl files wholesale from bot to dashboard.
    Returns list of file names that were updated (content changed)."""
    updated = []
    for fname in SUGGESTION_FILES:
        src = BOT_ROOT / "journal" / fname
        dst = DASHBOARD_ROOT / "journal" / fname
        if not src.exists():
            continue
        src_bytes = src.read_bytes()
        if dst.exists() and dst.read_bytes() == src_bytes:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src_bytes)
        updated.append(fname)
    return updated


def main() -> int:
    if not BOT_JOURNAL.exists():
        print("[journal-push] no bot journal found, skipping")
        return 0

    # H-2: heal a broken main ref before doing any git work, so a prior failed
    # push can't silently freeze the dashboard for days.
    if not _ensure_repo_healthy():
        print("[journal-push] repo unhealthy and could not auto-repair — skipping push this cycle")
        return 1

    bot_entries  = _load_jsonl(BOT_JOURNAL)
    dash_entries = _load_jsonl(DASH_JOURNAL)

    existing_ts  = {e.get("timestamp") for e in dash_entries if e.get("timestamp")}
    new_entries  = [e for e in bot_entries if e.get("timestamp") not in existing_ts]

    sugg_updated = _sync_suggestions()

    if not new_entries and not sugg_updated:
        print(f"[journal-push] no new entries ({len(bot_entries)} in bot journal, all synced)")
        return 0

    if new_entries:
        DASH_JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        with DASH_JOURNAL.open("a", encoding="utf-8") as f:
            for entry in new_entries:
                f.write(json.dumps(entry) + "\n")
        print(f"[journal-push] appended {len(new_entries)} new entr{'y' if len(new_entries) == 1 else 'ies'} to dashboard journal")

    if sugg_updated:
        print(f"[journal-push] updated suggestion files: {', '.join(sugg_updated)}")

    _git("add", "journal/trades.jsonl")
    for fname in sugg_updated:
        _git("add", f"journal/{fname}")

    if _git("diff", "--cached", "--quiet").returncode == 0:
        print("[journal-push] git diff empty after add — nothing to commit")
        return 0

    now_utc = datetime.now(timezone.utc).isoformat()
    parts = []
    if new_entries:
        parts.append(f"+{len(new_entries)} entries")
    if sugg_updated:
        parts.append(f"suggestions: {', '.join(sugg_updated)}")
    msg = f"chore: journal sync {now_utc} ({'; '.join(parts)})"
    cm = _git("commit", "-m", msg)
    if cm.returncode != 0:
        print(f"[journal-push] commit failed: {cm.stderr.strip()}")
        return 1

    if not _ensure_gh_user():
        print(f"[journal-push] warning: could not switch gh to {GH_USER}, attempting push anyway")

    # Pull remote changes first to avoid non-fast-forward rejections.
    # Use --no-rebase (merge) so concurrent CI appends to trades.jsonl don't
    # produce rebase conflicts — a merge commit on an append-only file is safe.
    pull = _git("pull", "--no-rebase")
    if pull.returncode != 0:
        print(f"[journal-push] pull failed: {pull.stderr.strip()}")

    push = _git("push")
    if push.returncode != 0:
        print(f"[journal-push] push failed: {push.stderr.strip()}")
        return 1

    print(f"[journal-push] pushed {len(new_entries)} entries to dashboard repo (origin)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
