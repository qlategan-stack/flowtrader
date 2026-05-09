"""
CI-side journal sync: merges this run's journal entries into a clone of the
flowtrader-dashboard repo so the Streamlit dashboard can display them.

Local devs use scripts/push_journal.py instead — it relies on a sibling repo
checkout and the gh CLI. CI doesn't have either, so this script takes an
explicit --dashboard-path and is invoked by the GitHub Actions workflow
after the dashboard repo has been cloned with a write-capable token.

Behaviour:
  * Dedups trades.jsonl entries by `timestamp` so re-runs never duplicate.
  * Replaces bybit_balance.json wholesale (it's a snapshot, not a log).
  * Exits 0 with nothing to do if no new content to write.
  * Exits 1 on hard error.

The workflow uses the script's stdout to decide whether to commit, by checking
whether any of the target files are dirty in `git status` after this script
runs. We don't use exit codes to signal change — that creates fragile shell
chains. The git diff is the source of truth.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def sync_trades(bot_journal: Path, dash_journal: Path) -> int:
    """Append new entries from bot_journal to dash_journal. Returns count appended."""
    if not bot_journal.exists():
        print(f"[ci-sync] no bot trades.jsonl at {bot_journal}")
        return 0

    bot_entries  = _load_jsonl(bot_journal)
    dash_entries = _load_jsonl(dash_journal)

    existing_ts = {e.get("timestamp") for e in dash_entries if e.get("timestamp")}
    new_entries = [e for e in bot_entries if e.get("timestamp") not in existing_ts]

    if not new_entries:
        print(f"[ci-sync] trades.jsonl: no new entries ({len(bot_entries)} in bot, {len(dash_entries)} in dash)")
        return 0

    dash_journal.parent.mkdir(parents=True, exist_ok=True)
    with dash_journal.open("a", encoding="utf-8") as f:
        for e in new_entries:
            f.write(json.dumps(e) + "\n")

    print(f"[ci-sync] trades.jsonl: appended {len(new_entries)} new entr{'y' if len(new_entries) == 1 else 'ies'}")
    return len(new_entries)


def sync_bybit_balance(bot_path: Path, dash_path: Path) -> bool:
    """Copy the latest bybit_balance.json snapshot. Returns True if file changed."""
    if not bot_path.exists():
        print(f"[ci-sync] no bot bybit_balance.json at {bot_path} — skipping")
        return False

    if dash_path.exists() and dash_path.read_bytes() == bot_path.read_bytes():
        print("[ci-sync] bybit_balance.json: unchanged")
        return False

    dash_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bot_path, dash_path)
    print("[ci-sync] bybit_balance.json: replaced")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dashboard-path",
        required=True,
        help="Path to a checkout of qlategan-stack/flowtrader-dashboard",
    )
    args = parser.parse_args()

    bot_root  = Path.cwd()
    dash_root = Path(args.dashboard_path).resolve()

    if not dash_root.exists():
        print(f"[ci-sync] FATAL: dashboard path does not exist: {dash_root}")
        return 1

    sync_trades(
        bot_journal  = bot_root  / "journal" / "trades.jsonl",
        dash_journal = dash_root / "journal" / "trades.jsonl",
    )

    sync_bybit_balance(
        bot_path  = bot_root  / "journal" / "bybit_balance.json",
        dash_path = dash_root / "journal" / "bybit_balance.json",
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
