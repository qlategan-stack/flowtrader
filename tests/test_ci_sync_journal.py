"""
Tests for scripts.ci_sync_journal — the CI-side journal merge logic.
"""
import json
import sys
from pathlib import Path

import pytest

# Allow `from ci_sync_journal import ...` regardless of where pytest is run from
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from ci_sync_journal import sync_trades  # noqa: E402


@pytest.fixture
def bot_dash(tmp_path):
    bot_root  = tmp_path / "bot"
    dash_root = tmp_path / "dash"
    (bot_root  / "journal").mkdir(parents=True)
    (dash_root / "journal").mkdir(parents=True)
    return bot_root, dash_root


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(e) for e in entries) + ("\n" if entries else ""),
        encoding="utf-8",
    )


# ── sync_trades ───────────────────────────────────────────────────────────────

def test_sync_trades_appends_new_entries(bot_dash):
    bot_root, dash_root = bot_dash
    bot = bot_root  / "journal" / "trades.jsonl"
    dash = dash_root / "journal" / "trades.jsonl"

    _write_jsonl(bot, [
        {"timestamp": "2026-05-09T10:00:00Z", "action": "BUY",  "symbol": "META"},
        {"timestamp": "2026-05-09T10:30:00Z", "action": "SKIP", "symbol": None},
    ])
    _write_jsonl(dash, [
        {"timestamp": "2026-05-09T10:00:00Z", "action": "BUY", "symbol": "META"},
    ])

    appended = sync_trades(bot, dash)
    assert appended == 1

    final = [json.loads(l) for l in dash.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(final) == 2
    assert final[-1]["timestamp"] == "2026-05-09T10:30:00Z"


def test_sync_trades_creates_dash_file_when_missing(bot_dash):
    bot_root, dash_root = bot_dash
    bot = bot_root / "journal" / "trades.jsonl"
    dash = dash_root / "journal" / "trades.jsonl"  # does not exist yet

    _write_jsonl(bot, [
        {"timestamp": "2026-05-09T10:00:00Z", "action": "BUY", "symbol": "META"},
    ])

    appended = sync_trades(bot, dash)
    assert appended == 1
    assert dash.exists()


def test_sync_trades_no_op_when_all_already_synced(bot_dash):
    bot_root, dash_root = bot_dash
    bot = bot_root / "journal" / "trades.jsonl"
    dash = dash_root / "journal" / "trades.jsonl"

    entries = [{"timestamp": "2026-05-09T10:00:00Z", "action": "BUY", "symbol": "META"}]
    _write_jsonl(bot, entries)
    _write_jsonl(dash, entries)

    appended = sync_trades(bot, dash)
    assert appended == 0

    # File content unchanged
    assert dash.read_text(encoding="utf-8").count("META") == 1


def test_sync_trades_no_bot_file_returns_zero(bot_dash):
    bot_root, dash_root = bot_dash
    bot = bot_root / "journal" / "trades.jsonl"  # never created
    dash = dash_root / "journal" / "trades.jsonl"
    _write_jsonl(dash, [{"timestamp": "2026-05-09T10:00:00Z"}])

    assert sync_trades(bot, dash) == 0


def test_sync_trades_skips_corrupt_lines(bot_dash):
    bot_root, dash_root = bot_dash
    bot = bot_root / "journal" / "trades.jsonl"
    dash = dash_root / "journal" / "trades.jsonl"
    bot.parent.mkdir(parents=True, exist_ok=True)
    bot.write_text(
        '{"timestamp": "2026-05-09T10:00:00Z", "action": "BUY"}\n'
        'corrupt line not json{{{\n'
        '{"timestamp": "2026-05-09T10:30:00Z", "action": "SKIP"}\n',
        encoding="utf-8",
    )

    assert sync_trades(bot, dash) == 2


# sync_bybit_balance was removed 2026-05-11 — local push_bybit_balance.py is
# the sole writer to journal/bybit_balance.json. See trading-bot.yml step 8.
