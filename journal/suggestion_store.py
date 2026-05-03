"""
Shared JSONL persistence for analyst suggestions.
Used by both InStrategyAnalyst and OutStrategyAnalyst.
"""
# journal/suggestion_store.py
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_UPDATEABLE_FIELDS = [
    "title", "analysis", "rationale", "insight",
    "current_rule", "proposed_rule", "proposed_claude_md_diff",
    "confidence", "supporting_data", "generated_at", "priority",
]

class SuggestionStore:

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        records = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning("Skipping malformed suggestion record")
        return records

    def find_pending_by_category(self, category: str) -> Optional[dict]:
        for record in self.load_all():
            if record.get("category") == category and record.get("status") == "pending":
                return record
        return None

    def append(self, suggestion: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(suggestion) + "\n")

    def upsert(self, suggestion: dict) -> str:
        existing = self.find_pending_by_category(suggestion["category"])
        if existing:
            updates = {k: suggestion[k] for k in _UPDATEABLE_FIELDS if k in suggestion}
            self.update(existing["id"], updates)
            logger.info(f"Updated pending suggestion {existing['id']} for category {suggestion['category']}")
            return existing["id"]
        self.append(suggestion)
        logger.info(f"Created suggestion {suggestion['id']} for category {suggestion['category']}")
        return suggestion["id"]

    def update(self, suggestion_id: str, updates: dict) -> bool:
        records = self.load_all()
        found = False
        for record in records:
            if record["id"] == suggestion_id:
                record.update(updates)
                found = True
                break
        if not found:
            return False
        self._rewrite(records)
        return True

    def action(self, suggestion_id: str, action: str) -> bool:
        return self.update(suggestion_id, {
            "status": action,
            "actioned_at": datetime.now(timezone.utc).isoformat(),
            "actioned_by": action,
        })

    @staticmethod
    def apply_to_claude_md(claude_md_path: str, current_rule: str, proposed_rule: str) -> None:
        path = Path(claude_md_path)
        content = path.read_text(encoding="utf-8")
        if current_rule not in content:
            raise ValueError(f"Rule not found in CLAUDE.md: {current_rule!r}")
        updated = content.replace(current_rule, proposed_rule, 1)
        path.write_text(updated, encoding="utf-8")
        logger.info("CLAUDE.md patched successfully")

    def _rewrite(self, records: list[dict]) -> None:
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record) + "\n")
        tmp.replace(self.path)
