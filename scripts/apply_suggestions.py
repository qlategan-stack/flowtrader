"""
Apply approved suggestions to CLAUDE.md.

After approving a suggestion in the Streamlit dashboard, run:

    python scripts/apply_suggestions.py

This script:
  1. Pulls the latest suggestions_in.jsonl from the dashboard repo
     (where the dashboard wrote the status='approved' update).
  2. For each approved suggestion not yet applied, replaces current_rule
     with proposed_rule in CLAUDE.md.
  3. Marks the suggestion as 'applied' in suggestions_in.jsonl so it
     won't be applied twice.
  4. Optionally commits the CLAUDE.md change.

Use --dry-run to preview without writing.
"""
import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BOT_ROOT       = Path(__file__).resolve().parent.parent
DASHBOARD_ROOT = BOT_ROOT.parent.parent / "flowtrader-dashboard"

CLAUDE_MD              = BOT_ROOT / "CLAUDE.md"
BOT_SUGGESTIONS        = BOT_ROOT / "journal" / "suggestions_in.jsonl"
DASHBOARD_SUGGESTIONS  = DASHBOARD_ROOT / "journal" / "suggestions_in.jsonl"


def load_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"[apply] skipping malformed line in {path.name}")
    return out


def write_records(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(json.dumps(r) for r in records) + "\n"
    path.write_text(content, encoding="utf-8")


def sync_from_dashboard() -> None:
    """Pull suggestion status updates from dashboard repo into bot repo."""
    if not DASHBOARD_SUGGESTIONS.exists():
        print(f"[apply] no dashboard suggestions file at {DASHBOARD_SUGGESTIONS}")
        return
    subprocess.run(
        ["git", "pull", "--rebase", "--autostash"],
        cwd=DASHBOARD_ROOT, capture_output=True, text=True
    )
    shutil.copy2(DASHBOARD_SUGGESTIONS, BOT_SUGGESTIONS)
    print(f"[apply] synced {DASHBOARD_SUGGESTIONS} → {BOT_SUGGESTIONS}")


def apply_one(rec: dict, dry_run: bool) -> bool:
    """Apply a single approved suggestion to CLAUDE.md. Returns True if applied."""
    current = rec.get("current_rule", "")
    proposed = rec.get("proposed_rule", "")
    sid = rec.get("id", "?")
    title = rec.get("title", "")[:80]

    if not current or not proposed:
        print(f"[apply] {sid}: missing current_rule or proposed_rule — skipped")
        return False

    content = CLAUDE_MD.read_text(encoding="utf-8")
    if current not in content:
        print(f"[apply] {sid}: current_rule not found in CLAUDE.md — already applied or drifted")
        return False

    if dry_run:
        print(f"[apply] DRY RUN {sid}: would patch — {title}")
        return True

    updated = content.replace(current, proposed, 1)
    CLAUDE_MD.write_text(updated, encoding="utf-8")
    print(f"[apply] PATCHED {sid}: {title}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply approved suggestions to CLAUDE.md")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--no-sync", action="store_true", help="Skip pulling from dashboard repo")
    parser.add_argument("--commit",  action="store_true", help="Commit CLAUDE.md after patching")
    args = parser.parse_args()

    if not args.no_sync:
        sync_from_dashboard()

    records = load_records(BOT_SUGGESTIONS)
    if not records:
        print("[apply] no suggestions found")
        return 0

    to_apply = [r for r in records if r.get("status") == "approved"]
    if not to_apply:
        print(f"[apply] no approved suggestions awaiting application ({len(records)} total in store)")
        return 0

    print(f"[apply] {len(to_apply)} approved suggestion(s) to apply")
    applied_ids = []
    for rec in to_apply:
        if apply_one(rec, args.dry_run):
            applied_ids.append(rec["id"])

    if args.dry_run:
        print(f"[apply] dry-run complete — {len(applied_ids)} would be applied")
        return 0

    if not applied_ids:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    for r in records:
        if r["id"] in applied_ids:
            r["status"]       = "applied"
            r["applied_at"]   = now
    write_records(BOT_SUGGESTIONS, records)
    print(f"[apply] marked {len(applied_ids)} record(s) as applied in suggestions_in.jsonl")

    if args.commit:
        subprocess.run(["git", "add", "CLAUDE.md", "journal/suggestions_in.jsonl"], cwd=BOT_ROOT)
        ids_str = ", ".join(applied_ids)
        subprocess.run(
            ["git", "commit", "-m", f"feat(rules): apply approved suggestions ({ids_str})"],
            cwd=BOT_ROOT,
        )
        print(f"[apply] committed CLAUDE.md and suggestions_in.jsonl")

    return 0


if __name__ == "__main__":
    sys.exit(main())
