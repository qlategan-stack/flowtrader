"""
scripts/strategy_ab.py — Math-strategy A/B framework (L-3, audit 2026-06-10).

The 8 math strategies in journal/math_strategies.json have been enabled en masse
and never attributed, because attribution needs CLOSED round-trips (realised R)
which only started accumulating once the exit path was fixed (H-2). This tool:

  report   — per-strategy attribution from closed trades: how many closed trades
             fired each strategy's signal, and the avg realised R when it did vs
             didn't. Read-only.

  plan     — print the disable-one-at-a-time schedule the audit proposed (start
             with the most expensive-to-compute family), 14 days each.

  disable  — flip ONE strategy off in math_strategies.json and stamp the change,
             recording the experiment start so `report --since` can window on it.
             (Writes the dashboard-side file, the user-facing control surface.)

  enable   — re-enable a strategy (revert an experiment).

Attribution maps the signal-label prefixes the engine emits in signals_fired
(Wavelet:, Hurst=, Entropy:, LevyJump:) back to strategy keys. Multi-asset
families (transfer_entropy, rmt_correlation, wasserstein_regime, tda_features)
do not tag per-symbol signals_fired, so they are reported as NOT-ATTRIBUTABLE
from the journal alone — flagged explicitly rather than silently scored 0.

Usage:
    python scripts/strategy_ab.py report [--days 30]
    python scripts/strategy_ab.py plan
    python scripts/strategy_ab.py disable hurst_exponent
    python scripts/strategy_ab.py enable hurst_exponent
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_BOT_ROOT = Path(__file__).resolve().parent.parent
_DASH_STRAT = _BOT_ROOT.parent.parent / "flowtrader-dashboard" / "journal" / "math_strategies.json"
_LOCAL_STRAT = _BOT_ROOT / "journal" / "math_strategies.json"
_JOURNAL = _BOT_ROOT / "journal" / "trades.jsonl"

# Signal-label prefix -> strategy key. Mirrors strategies/engine.py emissions.
_LABEL_TO_KEY = {
    "Wavelet": "wavelet_denoising",
    "Hurst": "hurst_exponent",
    "Entropy": "entropy_regime",
    "LevyJump": "levy_jump",
}
# Multi-asset families that don't tag per-symbol signals_fired — can't be
# attributed from the journal, so we say so rather than imply zero edge.
_NOT_ATTRIBUTABLE = ["transfer_entropy", "rmt_correlation", "wasserstein_regime", "tda_features"]

# Audit's suggested order: most expensive to compute first.
_AB_ORDER = ["tda_features", "rmt_correlation", "wasserstein_regime", "transfer_entropy",
             "wavelet_denoising", "hurst_exponent", "entropy_regime", "levy_jump"]

_ALL_KEYS = list(_LABEL_TO_KEY.values()) + _NOT_ATTRIBUTABLE


def _strat_file() -> Path:
    """Prefer the dashboard file (user-facing control surface); fall back local."""
    return _DASH_STRAT if _DASH_STRAT.exists() else _LOCAL_STRAT


def _load_rows(days: int) -> list[dict]:
    if not _JOURNAL.exists():
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = []
    for line in _JOURNAL.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (r.get("date") or "") >= cutoff:
            rows.append(r)
    return rows


def _keys_in_signals(signals: list) -> set[str]:
    keys = set()
    for s in signals or []:
        head = str(s).replace("=", ":").split(":", 1)[0].strip()
        if head in _LABEL_TO_KEY:
            keys.add(_LABEL_TO_KEY[head])
    return keys


def _closed_with_signals(rows: list[dict]) -> list[dict]:
    """Join FILLED SELL closes (which carry realised_pl_approx) back to the
    BUY that opened the position, so the entry's signals_fired can be
    attributed to the realised P&L. Matched by symbol, most-recent prior BUY."""
    buys = [r for r in rows if r.get("action") == "BUY"
            and r.get("execution_status") in ("FILLED", "PARTIAL")]
    closes = [r for r in rows if r.get("action") == "SELL"
              and r.get("execution_status") == "FILLED"
              and r.get("realised_pl_approx") is not None]
    out = []
    for c in closes:
        sym = c.get("symbol")
        prior = [b for b in buys if b.get("symbol") == sym
                 and (b.get("timestamp") or "") <= (c.get("timestamp") or "")]
        entry = max(prior, key=lambda b: b.get("timestamp") or "") if prior else {}
        out.append({"pl": float(c["realised_pl_approx"]),
                    "signals": entry.get("signals_fired") or []})
    return out


def cmd_report(days: int) -> int:
    rows = _load_rows(days)
    closed = _closed_with_signals(rows)
    print(f"Closed round-trips with realised P&L in last {days}d: {len(closed)}")
    if not closed:
        print("  No closed trades yet — attribution is INCONCLUSIVE for every "
              "strategy (this is the H-2 precondition; let exits accumulate).")
    else:
        for key in _LABEL_TO_KEY.values():
            fired = [c for c in closed if key in _keys_in_signals(c["signals"])]
            absent = [c for c in closed if key not in _keys_in_signals(c["signals"])]
            def _avg(xs):
                return sum(x["pl"] for x in xs) / len(xs) if xs else None
            af, aa = _avg(fired), _avg(absent)
            edge = (af - aa) if (af is not None and aa is not None) else None
            print(f"  {key:20s} fired={len(fired):3d}  avgPL_fired="
                  f"{('$%.2f' % af) if af is not None else '   n/a':>9}  "
                  f"avgPL_absent={('$%.2f' % aa) if aa is not None else '   n/a':>9}  "
                  f"edge={('%+.2f' % edge) if edge is not None else ' n/a'}")
    print("\nNOT attributable from journal (multi-asset, no per-symbol tag):")
    print("  " + ", ".join(_NOT_ATTRIBUTABLE))
    print("  -> A/B these via the disable-one-at-a-time experiment (see `plan`).")
    return 0


def cmd_plan() -> int:
    print("Disable-one-at-a-time A/B plan (14 days each, most expensive first):")
    start = "after >=20 closed round-trips exist (H-2 precondition)"
    print(f"  Precondition: {start}\n")
    for i, key in enumerate(_AB_ORDER, 1):
        print(f"  {i}. disable {key:20s} for 14d -> compare avg R vs the enabled baseline")
    print("\n  KEEP if avg R drops with the family off; DISABLE permanently if "
          "avg R is unchanged or improves.")
    print("  Run:  python scripts/strategy_ab.py disable <key>   (then enable to revert)")
    return 0


def _set_enabled(key: str, enabled: bool) -> int:
    if key not in _ALL_KEYS:
        print(f"Unknown strategy '{key}'. Valid: {', '.join(_ALL_KEYS)}")
        return 1
    path = _strat_file()
    if not path.exists():
        print(f"No math_strategies.json at {path}")
        return 1
    raw = json.loads(path.read_text(encoding="utf-8"))
    strategies = raw.setdefault("strategies", {})
    strategies.setdefault(key, {})["enabled"] = enabled
    # Stamp the experiment so report windows can key off it. No Date.now in
    # scripts is fine here — this is a manual CLI tool, real wall-clock is OK.
    raw["updated_at"] = datetime.now(timezone.utc).isoformat()
    exp = raw.setdefault("_ab_experiments", {})
    exp[key] = {"enabled": enabled, "changed_at": raw["updated_at"]}
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    print(f"{'Enabled' if enabled else 'Disabled'} {key} in {path.name} "
          f"(updated_at {raw['updated_at']}).")
    print("Takes effect on the next bot run (engine reads the newer-stamped file).")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("report", help="per-strategy attribution from closed trades")
    pr.add_argument("--days", type=int, default=30)
    sub.add_parser("plan", help="print the disable-one-at-a-time A/B schedule")
    pd = sub.add_parser("disable", help="disable one strategy")
    pd.add_argument("strategy")
    pe = sub.add_parser("enable", help="enable one strategy")
    pe.add_argument("strategy")
    args = p.parse_args()

    if args.cmd == "report":
        return cmd_report(args.days)
    if args.cmd == "plan":
        return cmd_plan()
    if args.cmd == "disable":
        return _set_enabled(args.strategy, False)
    if args.cmd == "enable":
        return _set_enabled(args.strategy, True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
