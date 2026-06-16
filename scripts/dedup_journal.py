"""
One-time journal hygiene migration — H-5 / L-2 (audit 2026-06-10).

trades.jsonl accumulated two classes of cruft before the journal normaliser
(_normalize_execution_status) existed:

  L-2 — 103 pre-reconciliation "SUBMITTED" rows that each have a matching
        "_backfill": true "FILLED" row for the SAME order_id. Naive counts of
        "orders"/"fills" double-count these. The FILLED row is the terminal
        truth; the SUBMITTED row is dropped.

  H-5 — invalid execution_status values that leaked before normalisation:
        "ORDERSTATUS.FILLED" (Alpaca enum str() leak) -> "FILLED"
        "ERROR"              (unrecognised)            -> "REJECTED"

The script is IDEMPOTENT: re-running it on an already-clean journal is a no-op.
It writes a timestamped .bak alongside the journal before touching anything, and
writes atomically via a temp file + replace so a crash can't truncate the
journal the live bot appends to.

Usage:
    python scripts/dedup_journal.py            # apply
    python scripts/dedup_journal.py --dry-run  # report only, no write
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

JOURNAL = Path(__file__).resolve().parent.parent / "journal" / "trades.jsonl"

STATUS_REMAP = {
    "ORDERSTATUS.FILLED": "FILLED",   # H-5: Alpaca OrderStatus enum str() leak
    "ERROR": "REJECTED",              # H-5: surface unrecognised status as a terminal reject
}


def main(dry_run: bool = False) -> int:
    if not JOURNAL.exists():
        print(f"No journal at {JOURNAL} — nothing to do.")
        return 0

    rows: list[dict] = []
    malformed = 0
    for line in JOURNAL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            malformed += 1
    if malformed:
        print(f"WARNING: skipped {malformed} malformed line(s) — they will be DROPPED on write.")

    # L-2: collect order_ids that have a backfilled FILLED terminal record.
    backfilled_filled_ids = {
        r.get("order_id")
        for r in rows
        if r.get("_backfill") and r.get("execution_status") == "FILLED" and r.get("order_id")
    }

    out: list[dict] = []
    dropped_submitted = 0
    remapped = 0
    for r in rows:
        status = r.get("execution_status")
        oid = r.get("order_id")

        # L-2: drop SUBMITTED rows superseded by a backfilled FILLED for the same order.
        if status == "SUBMITTED" and oid and oid in backfilled_filled_ids:
            dropped_submitted += 1
            continue

        # H-5: normalise invalid status tokens.
        if status in STATUS_REMAP:
            r["execution_status"] = STATUS_REMAP[status]
            r.setdefault("_status_normalized_from", status)
            remapped += 1

        out.append(r)

    print(f"Read {len(rows)} rows.")
    print(f"  L-2: drop {dropped_submitted} SUBMITTED rows with a backfilled FILLED twin.")
    print(f"  H-5: remap {remapped} invalid-status rows ({', '.join(STATUS_REMAP)}).")
    print(f"  -> {len(out)} rows after cleanup.")

    if dropped_submitted == 0 and remapped == 0:
        print("Journal already clean — no changes.")
        return 0

    if dry_run:
        print("--dry-run: no files written.")
        return 0

    # Timestamped backup (stamp passed in argv-free via time is fine for a manual tool).
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = JOURNAL.with_suffix(f".jsonl.bak.{stamp}")
    shutil.copy2(JOURNAL, backup)
    print(f"Backup written: {backup.name}")

    # Atomic write: temp file in the same dir, then replace.
    tmp = JOURNAL.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(JOURNAL)
    print(f"Rewrote {JOURNAL.name} ({len(out)} rows).")
    return 0


if __name__ == "__main__":
    sys.exit(main(dry_run="--dry-run" in sys.argv))
