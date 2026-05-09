"""
Daily FlowTrader Trade Analysis report generator.

Reads journal entries from a flowtrader-dashboard checkout, finds failures
from the most recent trading day, optionally generates Claude-written lessons
per failure, and writes:

  <output-dir>/index.html         — full HTML report (vertical tab list)
  <status-path>                   — compact tradeflow_status.json for portals

Failures are partitioned into:
  * `loss` — closed positions where realised P&L < 0 (BUY paired with SELL
    of the same symbol, FIFO match)
  * `exec_error` — entries whose execution_status is ERROR or REJECTED

Successful trades are intentionally excluded — the user wants to learn from
mistakes first; that switches later.

Usage:
  python scripts/generate_trade_analysis.py \\
      --journal /tmp/dashboard/journal/trades.jsonl \\
      --output-dir /tmp/dashboard/trade-analysis \\
      --status-path /tmp/dashboard/tradeflow_status.json \\
      [--no-claude]   # skip Claude lesson generation (testing / cost-saving)

Anthropic API key (when not --no-claude): ANTHROPIC_API_KEY env var.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import escape as html_escape
from pathlib import Path
from typing import Optional

logger = logging.getLogger("trade-analysis")

CLAUDE_MODEL = "claude-sonnet-4-6"
LESSON_MAX_TOKENS = 220


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class Failure:
    kind: str                       # "loss" | "exec_error"
    symbol: str
    date: str
    time_est: str
    timestamp: str

    # Common
    signal_score: Optional[int] = None
    signals_fired: list = field(default_factory=list)
    confidence: Optional[str] = None
    reasoning: str = ""

    # Loss-specific
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    quantity: Optional[float] = None
    realized_pl: Optional[float] = None
    pl_pct: Optional[float] = None
    entry_time: Optional[str] = None
    exit_time: Optional[str] = None

    # Error-specific
    rejection_reason: str = ""
    api_error_kind: Optional[str] = None

    # Filled by Claude
    lesson: str = ""


# ── Loading ──────────────────────────────────────────────────────────────────

def load_jsonl(path: Path) -> list[dict]:
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


# ── Failure detection ────────────────────────────────────────────────────────

def latest_trading_date(entries: list[dict]) -> Optional[str]:
    """Most recent date in the journal with at least one action."""
    dates = sorted({e.get("date") for e in entries if e.get("date") and e.get("action")})
    return dates[-1] if dates else None


def most_recent_failure_date(entries: list[dict]) -> Optional[str]:
    """
    Most recent date that contains a real failure (closing loss or exec error).
    Returns None if there are no failures anywhere in the journal. Used as the
    default report date so the report always lands on a useful day.
    """
    closed = pair_buys_with_sells(entries)
    losses_dates = {
        t["sell"].get("date") for t in closed
        if t["realized_pl"] < 0 and t["sell"].get("date")
    }
    error_dates = {
        e.get("date") for e in entries
        if e.get("date") and (e.get("execution_status") or "").upper() in {"ERROR", "REJECTED"}
    }
    candidates = losses_dates | error_dates
    return max(candidates) if candidates else None


def pair_buys_with_sells(entries: list[dict]) -> list[dict]:
    """
    Pair each successful SELL with the most recent successful BUY of the same
    symbol that happened before it. Returns a list of closed-trade dicts:
      {symbol, buy: <entry>, sell: <entry>, realized_pl, pl_pct, qty}

    FIFO match by timestamp. If a SELL has no preceding unmatched BUY, it's
    skipped (already-closed or bot-bug).

    A trade is "closed" only if both legs have execution_status in
    {SUBMITTED, FILLED, SIMULATED} — not ERROR/REJECTED.
    """
    OK = {"SUBMITTED", "FILLED", "SIMULATED"}
    chronological = sorted(entries, key=lambda e: e.get("timestamp") or "")

    open_buys: dict[str, list[dict]] = {}       # symbol -> [unmatched BUYs, oldest first]
    closed_trades: list[dict] = []

    for e in chronological:
        action = (e.get("action") or "").upper()
        status = (e.get("execution_status") or "").upper()
        sym    = e.get("symbol")
        if not sym or status not in OK:
            continue

        if action == "BUY":
            open_buys.setdefault(sym, []).append(e)
        elif action == "SELL":
            queue = open_buys.get(sym) or []
            if not queue:
                continue
            buy = queue.pop(0)

            buy_qty   = float(buy.get("quantity") or 0)
            sell_qty  = float(e.get("quantity") or 0)
            qty       = min(buy_qty, sell_qty) if buy_qty and sell_qty else (buy_qty or sell_qty)
            buy_px    = float(buy.get("entry_price") or 0)
            sell_px   = float(e.get("entry_price") or 0)
            pl        = (sell_px - buy_px) * qty
            pl_pct    = ((sell_px - buy_px) / buy_px * 100) if buy_px else 0.0

            closed_trades.append({
                "symbol":      sym,
                "buy":         buy,
                "sell":        e,
                "realized_pl": pl,
                "pl_pct":      pl_pct,
                "qty":         qty,
            })

    return closed_trades


def find_failures(entries: list[dict], target_date: str) -> list[Failure]:
    """
    Build the failure list for the given date:
      - Closed trades that lost money (realized_pl < 0), where the SELL was
        on `target_date`. The BUY may have been earlier — that's fine.
      - Execution errors on `target_date` (status ERROR or REJECTED).

    Returns failures sorted by time, losses first then errors within each
    timestamp.
    """
    failures: list[Failure] = []

    # Closed losses
    for trade in pair_buys_with_sells(entries):
        sell = trade["sell"]
        if sell.get("date") != target_date:
            continue
        if trade["realized_pl"] >= 0:
            continue  # Profitable trade — skip per spec
        buy = trade["buy"]
        failures.append(Failure(
            kind          = "loss",
            symbol        = trade["symbol"],
            date          = sell.get("date", ""),
            time_est      = sell.get("time_est", ""),
            timestamp     = sell.get("timestamp", ""),
            signal_score  = buy.get("signal_score"),
            signals_fired = buy.get("signals_fired") or [],
            confidence    = buy.get("confidence"),
            reasoning     = (buy.get("reasoning") or "")[:600],
            entry_price   = float(buy.get("entry_price") or 0) or None,
            exit_price    = float(sell.get("entry_price") or 0) or None,
            quantity      = trade["qty"] or None,
            realized_pl   = trade["realized_pl"],
            pl_pct        = trade["pl_pct"],
            entry_time    = f"{buy.get('date','')} {buy.get('time_est','')}".strip(),
            exit_time     = f"{sell.get('date','')} {sell.get('time_est','')}".strip(),
        ))

    # Execution errors
    for e in entries:
        if e.get("date") != target_date:
            continue
        status = (e.get("execution_status") or "").upper()
        if status not in {"ERROR", "REJECTED"}:
            continue
        failures.append(Failure(
            kind             = "exec_error",
            symbol           = e.get("symbol") or "—",
            date             = e.get("date", ""),
            time_est         = e.get("time_est", ""),
            timestamp        = e.get("timestamp", ""),
            signal_score     = e.get("signal_score"),
            signals_fired    = e.get("signals_fired") or [],
            confidence       = e.get("confidence"),
            reasoning        = (e.get("reasoning") or "")[:600],
            entry_price      = float(e.get("entry_price") or 0) or None,
            quantity         = float(e.get("quantity") or 0) or None,
            rejection_reason = e.get("rejection_reason") or "",
            api_error_kind   = e.get("api_error_kind"),
        ))

    failures.sort(key=lambda f: (f.timestamp, 0 if f.kind == "loss" else 1))
    return failures


# ── 7-day summary ────────────────────────────────────────────────────────────

def seven_day_summary(entries: list[dict], end_date: str) -> dict:
    """Stats over the last 7 calendar days ending on end_date inclusive."""
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
    start_dt = end_dt - timedelta(days=6)
    start = start_dt.isoformat()

    in_window = [e for e in entries if start <= (e.get("date") or "") <= end_date]

    closed = pair_buys_with_sells(in_window)
    losses = [t for t in closed if t["realized_pl"] < 0]
    wins   = [t for t in closed if t["realized_pl"] > 0]

    exec_errors = sum(
        1 for e in in_window
        if (e.get("execution_status") or "").upper() in {"ERROR", "REJECTED"}
    )

    total_realized = sum(t["realized_pl"] for t in closed)
    total_loss     = sum(t["realized_pl"] for t in losses)

    return {
        "start_date":       start,
        "end_date":         end_date,
        "closed_trades":    len(closed),
        "wins":             len(wins),
        "losses":           len(losses),
        "win_rate_pct":     (100.0 * len(wins) / len(closed)) if closed else None,
        "exec_errors":      exec_errors,
        "total_realized":   total_realized,
        "total_loss":       total_loss,
    }


# ── Claude lesson ────────────────────────────────────────────────────────────

LESSON_PROMPT = """You are reviewing a failed trade for a mean-reversion trading bot. Help the trader who is reading this report learn from it.

Trade details:
{detail}

Write a 2-sentence lesson. First sentence: what went wrong or what the bot got right despite the loss. Second sentence: a concrete adjustment or what to watch for next time. No preamble, no headings, no list bullets — just the two sentences. Speak directly to the trader."""


def _failure_detail_block(f: Failure) -> str:
    lines = [
        f"Kind: {f.kind}",
        f"Symbol: {f.symbol}",
        f"Date/time: {f.date} {f.time_est} EST",
    ]
    if f.kind == "loss":
        lines += [
            f"Entry: ${f.entry_price:.2f} on {f.entry_time}" if f.entry_price else "Entry: unknown",
            f"Exit: ${f.exit_price:.2f} on {f.exit_time}"   if f.exit_price  else "Exit: unknown",
            f"Quantity: {f.quantity}"                        if f.quantity    else "Quantity: unknown",
            f"Realized P&L: ${f.realized_pl:+,.2f} ({f.pl_pct:+.2f}%)" if f.realized_pl is not None else "",
            f"Signal score at entry: {f.signal_score}/6" if f.signal_score is not None else "",
            f"Signals fired at entry: {', '.join(f.signals_fired) if f.signals_fired else 'none'}",
            f"Entry confidence: {f.confidence}" if f.confidence else "",
        ]
    else:
        lines += [
            f"Execution status: error/rejected",
            f"Reason: {f.rejection_reason}" if f.rejection_reason else "",
            f"API error kind: {f.api_error_kind}" if f.api_error_kind else "",
            f"Signal score: {f.signal_score}" if f.signal_score is not None else "",
        ]
    if f.reasoning:
        lines.append(f"Bot's pre-trade reasoning excerpt: {f.reasoning[:300]}")
    return "\n".join(l for l in lines if l)


def generate_lesson(failure: Failure, client) -> str:
    """Call Claude and return a 2-sentence lesson. Falls back to empty string on error."""
    detail = _failure_detail_block(failure)
    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=LESSON_MAX_TOKENS,
            messages=[{"role": "user", "content": LESSON_PROMPT.format(detail=detail)}],
        )
        text = resp.content[0].text.strip()
        # Defend against the model sneaking in a preamble or list
        first_double_newline = text.find("\n\n")
        if first_double_newline > 0:
            text = text[:first_double_newline].strip()
        return text
    except Exception as e:
        logger.warning(f"Claude lesson failed for {failure.symbol} {failure.time_est}: {e}")
        return ""


# ── HTML rendering ───────────────────────────────────────────────────────────

def _h(s) -> str:
    return html_escape(str(s) if s is not None else "")


def _category_badge(kind: str) -> str:
    if kind == "loss":
        return '<span class="badge badge-loss">LOSS</span>'
    return '<span class="badge badge-error">EXEC ERROR</span>'


def _format_pl(pl: Optional[float], pct: Optional[float]) -> str:
    if pl is None:
        return "—"
    sign_pct = f" ({pct:+.2f}%)" if pct is not None else ""
    return f"${pl:+,.2f}{sign_pct}"


def _tab_label(f: Failure) -> str:
    """Short label for the vertical tab list."""
    sym = f.symbol or "—"
    if f.kind == "loss":
        pl = f"{f.realized_pl:+,.0f}" if f.realized_pl is not None else "—"
        return f"{sym} · {f.time_est[:5]} · {pl}"
    return f"{sym} · {f.time_est[:5]} · ERR"


def _render_panel(idx: int, f: Failure) -> str:
    """One detail panel for a single failure."""
    rows = []

    rows.append(f'<div class="panel-head">')
    rows.append(f'  <div class="panel-symbol">{_h(f.symbol)}</div>')
    rows.append(f'  <div class="panel-meta">{_category_badge(f.kind)} · {_h(f.date)} · {_h(f.time_est)} EST</div>')
    rows.append(f'</div>')

    if f.kind == "loss":
        rows.append('<div class="data-grid">')
        rows.append(f'  <div class="data-item"><div class="lbl">Entry</div><div class="val">${_h(f"{f.entry_price:.2f}")}</div><div class="sub">{_h(f.entry_time)}</div></div>')
        rows.append(f'  <div class="data-item"><div class="lbl">Exit</div><div class="val">${_h(f"{f.exit_price:.2f}")}</div><div class="sub">{_h(f.exit_time)}</div></div>')
        rows.append(f'  <div class="data-item"><div class="lbl">Qty</div><div class="val">{_h(f.quantity)}</div></div>')
        cls = "danger" if (f.realized_pl or 0) < 0 else "success"
        rows.append(f'  <div class="data-item data-pl {cls}"><div class="lbl">P&amp;L</div><div class="val">{_h(_format_pl(f.realized_pl, f.pl_pct))}</div></div>')
        rows.append('</div>')

        if f.signals_fired or f.signal_score is not None:
            rows.append('<div class="signals-row">')
            if f.signal_score is not None:
                rows.append(f'  <span class="chip chip-score">SCORE {_h(f.signal_score)}/6</span>')
            for s in (f.signals_fired or []):
                rows.append(f'  <span class="chip">{_h(s)}</span>')
            if f.confidence:
                rows.append(f'  <span class="chip chip-confidence">CONF {_h(f.confidence)}</span>')
            rows.append('</div>')
    else:
        rows.append('<div class="error-block">')
        rows.append(f'  <div class="error-reason">{_h(f.rejection_reason) or "(no reason given)"}</div>')
        if f.api_error_kind:
            rows.append(f'  <div class="error-kind">API kind: {_h(f.api_error_kind)}</div>')
        rows.append('</div>')

    if f.lesson:
        rows.append('<div class="lesson">')
        rows.append('  <div class="lesson-label">Lesson</div>')
        rows.append(f'  <div class="lesson-body">{_h(f.lesson)}</div>')
        rows.append('</div>')

    if f.reasoning:
        rows.append('<details class="reasoning">')
        rows.append('  <summary>Bot reasoning at the time</summary>')
        rows.append(f'  <pre>{_h(f.reasoning)}</pre>')
        rows.append('</details>')

    return f'<article class="panel{" active" if idx == 0 else ""}" id="f-{idx}"{"" if idx == 0 else " hidden"}>\n' + "\n".join(rows) + '\n</article>'


def _render_summary_card(s: dict) -> str:
    """Top 7-day summary block."""
    win_rate = f'{s["win_rate_pct"]:.0f}%' if s.get("win_rate_pct") is not None else "—"
    cards = [
        ("CLOSED TRADES", s["closed_trades"], "neutral"),
        ("WINS",          s["wins"],          "success"),
        ("LOSSES",        s["losses"],        "danger" if s["losses"] else "neutral"),
        ("WIN RATE",      win_rate,           "neutral"),
        ("EXEC ERRORS",   s["exec_errors"],   "warning" if s["exec_errors"] else "neutral"),
        ("REALISED P&L",  f"${s['total_realized']:+,.2f}", "success" if s["total_realized"] >= 0 else "danger"),
    ]
    items = "\n".join(
        f'<div class="kpi {tone}"><div class="kpi-lbl">{_h(lbl)}</div><div class="kpi-val">{_h(val)}</div></div>'
        for lbl, val, tone in cards
    )
    return f"""
<section class="summary">
  <div class="summary-head">
    <div class="summary-title">7-day window</div>
    <div class="summary-range">{_h(s['start_date'])} → {_h(s['end_date'])}</div>
  </div>
  <div class="kpi-grid">{items}</div>
</section>"""


def render_html(failures: list[Failure], summary: dict, report_date: str) -> str:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if not failures:
        body = f"""
<section class="empty-state">
  <div class="empty-icon">✓</div>
  <div class="empty-title">No failures on {_h(report_date)}</div>
  <div class="empty-sub">Markets cooperated. Keep watching for the patterns when they don't.</div>
</section>"""
    else:
        tabs_buttons = "\n".join(
            f'<button class="tab{" active" if i == 0 else ""}" '
            f'data-target="f-{i}" data-kind="{_h(f.kind)}">{_h(_tab_label(f))}</button>'
            for i, f in enumerate(failures)
        )
        panels = "\n".join(_render_panel(i, f) for i, f in enumerate(failures))
        body = f"""
<section class="failures">
  <div class="failures-head">
    <h2>Failures — {_h(report_date)}</h2>
    <div class="failures-count">{len(failures)} total</div>
  </div>
  <div class="tabs-wrap">
    <nav class="tabs-list" role="tablist">
{tabs_buttons}
    </nav>
    <div class="tab-panels">
{panels}
    </div>
  </div>
</section>"""

    summary_html = _render_summary_card(summary)

    return _HTML_TEMPLATE.format(
        title="FlowTrader — Trade Analysis",
        report_date=_h(report_date),
        generated_at=_h(generated_at),
        summary_block=summary_html,
        body=body,
    )


# ── HTML template (uses CLAUDE.md design tokens) ──────────────────────────────

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" class="theme-dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@300;400;700;800;900&family=Barlow:wght@300;400;500;600&display=swap" rel="stylesheet">
<script>var t=localStorage.getItem('ft-theme');if(t)document.documentElement.className=t;</script>
<style>
:root {{
  --_y50:#FEF9E0;--_y100:#FDF0A0;--_y200:#FAE04D;--_y400:#F5C400;--_y600:#D4A800;--_y800:#A88000;--_y900:#6A5000;
  --_n50:#E8EFF8;--_n100:#B8CCE8;--_n300:#6B9ED0;--_n500:#2D6BA8;--_n700:#1A3D6E;--_n900:#0D2040;--_n950:#071022;
  --_g0:#FFFFFF;--_g50:#F7F6F3;--_g100:#E8E7E2;--_g200:#C8C7C0;--_g400:#949390;--_g600:#5C5B58;--_g800:#2E2E2C;--_g900:#1A1A18;--_g950:#0D0D0B;
  --_teal:#2D8C7A;--_teal-light:#C8EDE7;--_teal-dark:#1a5c50;
  --_coral:#E86060;--_coral-light:#FDDCDC;
  --_terra:#C97A3A;
  --font-display:'Barlow Condensed',sans-serif;
  --font-body:'Barlow',sans-serif;
  --r-sm:4px;--r-md:8px;--r-lg:12px;
}}
.theme-light{{color-scheme:light;--color-surface-page:var(--_g50);--color-surface-base:var(--_g0);--color-surface-elevated:var(--_g0);--color-surface-sunken:var(--_g100);--color-surface-secondary:var(--_n700);--color-text-primary:var(--_g950);--color-text-secondary:var(--_g600);--color-text-tertiary:var(--_g400);--color-text-on-navy:var(--_g0);--color-brand-primary:var(--_y400);--color-border-default:var(--_g200);--color-border-subtle:var(--_g100);--color-success-bg:#EDF7F5;--color-success-fg:var(--_teal-dark);--color-warning-bg:var(--_y50);--color-warning-fg:var(--_y900);--color-danger-bg:#FEF2F2;--color-danger-fg:#C0392B;--color-neutral-fg:var(--_g600);--shadow-md:0 4px 12px rgba(0,0,0,.08);}}
.theme-dark{{color-scheme:dark;--color-surface-page:var(--_g950);--color-surface-base:var(--_g900);--color-surface-elevated:var(--_g800);--color-surface-sunken:var(--_g950);--color-surface-secondary:var(--_n700);--color-text-primary:var(--_g100);--color-text-secondary:var(--_g400);--color-text-tertiary:var(--_g600);--color-text-on-navy:var(--_g0);--color-brand-primary:var(--_y400);--color-border-default:rgba(255,255,255,.10);--color-border-subtle:rgba(255,255,255,.06);--color-success-bg:rgba(45,140,122,.14);--color-success-fg:var(--_teal-light);--color-warning-bg:rgba(245,196,0,.10);--color-warning-fg:var(--_y200);--color-danger-bg:rgba(232,96,96,.14);--color-danger-fg:var(--_coral-light);--color-neutral-fg:var(--_g400);--shadow-md:0 4px 12px rgba(0,0,0,.40);}}
.theme-brand{{color-scheme:light;--color-surface-page:var(--_y400);--color-surface-base:var(--_y200);--color-surface-elevated:var(--_y50);--color-surface-sunken:var(--_y600);--color-surface-secondary:var(--_g950);--color-text-primary:var(--_g950);--color-text-secondary:var(--_y900);--color-text-tertiary:var(--_y800);--color-text-on-navy:var(--_g0);--color-brand-primary:var(--_g950);--color-border-default:rgba(0,0,0,.14);--color-border-subtle:rgba(0,0,0,.08);--color-success-bg:rgba(45,140,122,.14);--color-success-fg:var(--_teal-dark);--color-warning-bg:rgba(0,0,0,.08);--color-warning-fg:var(--_y900);--color-danger-bg:rgba(232,96,96,.14);--color-danger-fg:#C0392B;--color-neutral-fg:var(--_y900);--shadow-md:0 4px 12px rgba(0,0,0,.14);}}
.theme-navy{{color-scheme:dark;--color-surface-page:var(--_n950);--color-surface-base:var(--_n900);--color-surface-elevated:var(--_n700);--color-surface-sunken:var(--_n950);--color-surface-secondary:var(--_n700);--color-text-primary:var(--_g0);--color-text-secondary:var(--_n100);--color-text-tertiary:var(--_n300);--color-text-on-navy:var(--_g0);--color-brand-primary:var(--_y400);--color-border-default:rgba(107,158,208,.20);--color-border-subtle:rgba(107,158,208,.12);--color-success-bg:rgba(45,140,122,.16);--color-success-fg:var(--_teal-light);--color-warning-bg:rgba(245,196,0,.12);--color-warning-fg:var(--_y200);--color-danger-bg:rgba(232,96,96,.16);--color-danger-fg:var(--_coral-light);--color-neutral-fg:var(--_n300);--shadow-md:0 4px 12px rgba(0,0,0,.50);}}

*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:var(--font-body);background:var(--color-surface-page);color:var(--color-text-primary);min-height:100vh;font-size:14px;line-height:1.5}}
.theme-bar{{display:flex;gap:4px;padding:6px 32px;background:var(--color-surface-secondary);border-bottom:1px solid var(--color-border-subtle)}}
.theme-bar button{{background:transparent;border:1px solid transparent;color:var(--color-text-on-navy);opacity:.55;padding:4px 12px;border-radius:var(--r-sm);font-family:var(--font-body);font-size:11px;font-weight:600;cursor:pointer;transition:all .15s}}
.theme-bar button:hover{{opacity:1}}
.theme-bar button.active{{opacity:1;border-color:var(--color-brand-primary);color:var(--color-brand-primary)}}

.hdr{{background:var(--color-surface-secondary);padding:24px 32px;border-bottom:2px solid var(--color-brand-primary);box-shadow:var(--shadow-md)}}
.hdr-title{{font-family:var(--font-display);font-size:30px;font-weight:900;text-transform:uppercase;letter-spacing:.04em;color:var(--color-text-on-navy)}}
.hdr-title .accent{{color:var(--color-brand-primary)}}
.hdr-meta{{font-size:12px;color:rgba(255,255,255,.5);margin-top:4px;display:flex;gap:14px;flex-wrap:wrap}}
.hdr-meta .pill{{background:rgba(255,255,255,.05);padding:2px 10px;border-radius:var(--r-pill,50px);border:1px solid rgba(255,255,255,.08)}}

main{{padding:24px 32px;max-width:1400px;margin:0 auto}}

.summary{{margin-bottom:28px}}
.summary-head{{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:10px}}
.summary-title{{font-family:var(--font-display);font-size:13px;font-weight:800;letter-spacing:.10em;text-transform:uppercase;color:var(--color-text-tertiary)}}
.summary-range{{font-size:12px;color:var(--color-text-tertiary)}}
.kpi-grid{{display:grid;grid-template-columns:repeat(6,1fr);gap:12px}}
@media (max-width:900px){{.kpi-grid{{grid-template-columns:repeat(2,1fr)}}}}
.kpi{{background:var(--color-surface-base);border:1px solid var(--color-border-subtle);border-radius:var(--r-md);padding:12px 14px}}
.kpi-lbl{{font-family:var(--font-display);font-size:10px;font-weight:700;letter-spacing:.10em;text-transform:uppercase;color:var(--color-text-tertiary);margin-bottom:6px}}
.kpi-val{{font-family:var(--font-display);font-size:22px;font-weight:900;color:var(--color-text-primary)}}
.kpi.success .kpi-val{{color:var(--color-success-fg)}}
.kpi.danger  .kpi-val{{color:var(--color-danger-fg)}}
.kpi.warning .kpi-val{{color:var(--color-warning-fg)}}

.failures-head{{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:14px}}
.failures-head h2{{font-family:var(--font-display);font-size:22px;font-weight:800;text-transform:uppercase;letter-spacing:.04em}}
.failures-count{{font-size:12px;color:var(--color-text-tertiary)}}

.tabs-wrap{{display:grid;grid-template-columns:280px 1fr;gap:18px;background:var(--color-surface-base);border:1px solid var(--color-border-subtle);border-radius:var(--r-md);overflow:hidden;min-height:480px}}
@media (max-width:800px){{.tabs-wrap{{grid-template-columns:1fr;min-height:auto}}}}
.tabs-list{{background:var(--color-surface-sunken);border-right:1px solid var(--color-border-subtle);max-height:640px;overflow-y:auto;padding:8px}}
@media (max-width:800px){{.tabs-list{{max-height:240px;border-right:none;border-bottom:1px solid var(--color-border-subtle)}}}}
.tab{{display:block;width:100%;text-align:left;background:transparent;border:none;border-left:3px solid transparent;color:var(--color-text-secondary);padding:10px 12px;font-family:var(--font-body);font-size:12.5px;cursor:pointer;border-radius:0 var(--r-sm) var(--r-sm) 0;transition:all .15s;font-weight:500}}
.tab:hover{{background:var(--color-surface-overlay,rgba(255,255,255,.04));color:var(--color-text-primary)}}
.tab.active{{background:var(--color-surface-base);color:var(--color-text-primary);font-weight:700}}
.tab[data-kind="loss"].active{{border-left-color:var(--color-danger-fg)}}
.tab[data-kind="exec_error"].active{{border-left-color:var(--color-warning-fg)}}
.tab[data-kind="loss"]{{border-left-color:rgba(232,96,96,.28)}}
.tab[data-kind="exec_error"]{{border-left-color:rgba(245,196,0,.28)}}
.tab-panels{{padding:20px 24px;overflow-y:auto;max-height:640px}}
.panel[hidden]{{display:none}}

.panel-head{{margin-bottom:16px}}
.panel-symbol{{font-family:var(--font-display);font-size:28px;font-weight:900;letter-spacing:.02em}}
.panel-meta{{font-size:11.5px;color:var(--color-text-tertiary);margin-top:4px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
.badge{{display:inline-block;padding:2px 8px;border-radius:var(--r-pill,50px);font-family:var(--font-display);font-size:9.5px;font-weight:800;letter-spacing:.10em}}
.badge-loss{{background:var(--color-danger-bg);color:var(--color-danger-fg)}}
.badge-error{{background:var(--color-warning-bg);color:var(--color-warning-fg)}}

.data-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px}}
@media (max-width:600px){{.data-grid{{grid-template-columns:repeat(2,1fr)}}}}
.data-item{{background:var(--color-surface-elevated);border:1px solid var(--color-border-subtle);border-radius:var(--r-sm);padding:10px 12px}}
.data-item .lbl{{font-family:var(--font-display);font-size:9.5px;font-weight:700;letter-spacing:.10em;text-transform:uppercase;color:var(--color-text-tertiary);margin-bottom:4px}}
.data-item .val{{font-family:var(--font-display);font-size:18px;font-weight:800;color:var(--color-text-primary)}}
.data-item .sub{{font-size:10.5px;color:var(--color-text-tertiary);margin-top:2px}}
.data-pl.danger .val{{color:var(--color-danger-fg)}}
.data-pl.success .val{{color:var(--color-success-fg)}}

.signals-row{{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}}
.chip{{display:inline-block;padding:3px 9px;border-radius:var(--r-pill,50px);background:var(--color-surface-elevated);border:1px solid var(--color-border-subtle);font-size:11px;font-weight:600;color:var(--color-text-secondary)}}
.chip-score{{background:var(--color-surface-secondary);color:var(--color-brand-primary);border-color:var(--color-brand-primary)}}
.chip-confidence{{background:transparent;border-color:var(--color-border-default)}}

.error-block{{background:var(--color-warning-bg);border:1px solid var(--color-warning-fg);border-radius:var(--r-sm);padding:12px 14px;margin-bottom:14px}}
.error-reason{{font-family:var(--font-body);font-weight:600;color:var(--color-warning-fg);font-size:13.5px}}
.error-kind{{font-size:11px;color:var(--color-text-tertiary);margin-top:4px;font-family:var(--font-display);text-transform:uppercase;letter-spacing:.05em}}

.lesson{{background:var(--color-surface-elevated);border-left:3px solid var(--color-brand-primary);border-radius:0 var(--r-sm) var(--r-sm) 0;padding:12px 14px;margin-bottom:14px}}
.lesson-label{{font-family:var(--font-display);font-size:9.5px;font-weight:800;letter-spacing:.12em;text-transform:uppercase;color:var(--color-brand-primary);margin-bottom:6px}}
.lesson-body{{font-size:13.5px;line-height:1.55}}

.reasoning{{margin-top:10px}}
.reasoning summary{{cursor:pointer;font-size:11.5px;color:var(--color-text-tertiary);font-family:var(--font-display);text-transform:uppercase;letter-spacing:.06em;padding:6px 0}}
.reasoning summary:hover{{color:var(--color-text-secondary)}}
.reasoning pre{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;background:var(--color-surface-sunken);border:1px solid var(--color-border-subtle);border-radius:var(--r-sm);padding:10px;margin-top:6px;white-space:pre-wrap;word-break:break-word;max-height:240px;overflow-y:auto;color:var(--color-text-secondary)}}

.empty-state{{text-align:center;padding:64px 24px;background:var(--color-surface-base);border:1px solid var(--color-border-subtle);border-radius:var(--r-md)}}
.empty-icon{{font-size:48px;color:var(--color-success-fg);margin-bottom:12px;font-weight:300}}
.empty-title{{font-family:var(--font-display);font-size:20px;font-weight:800;text-transform:uppercase;letter-spacing:.04em;margin-bottom:6px}}
.empty-sub{{font-size:13px;color:var(--color-text-tertiary)}}

footer{{padding:20px 32px;font-size:11px;color:var(--color-text-tertiary);text-align:center;border-top:1px solid var(--color-border-subtle);margin-top:40px}}
</style>
</head>
<body>
<div class="theme-bar">
  <button onclick="ftTheme('theme-light',this)">Light</button>
  <button onclick="ftTheme('theme-dark',this)" class="active">Dark</button>
  <button onclick="ftTheme('theme-brand',this)">Brand</button>
  <button onclick="ftTheme('theme-navy',this)">Navy</button>
</div>

<header class="hdr">
  <div class="hdr-title">FlowTrader <span class="accent">·</span> Trade Analysis</div>
  <div class="hdr-meta">
    <span class="pill">Report date: {report_date}</span>
    <span class="pill">Generated: {generated_at}</span>
    <span class="pill">Failures-only view</span>
  </div>
</header>

<main>
{summary_block}
{body}
</main>

<footer>FlowTrader trade-analysis · regenerated daily</footer>

<script>
const FT_THEMES=['theme-light','theme-dark','theme-brand','theme-navy'];
function ftTheme(t,btn){{document.documentElement.classList.remove(...FT_THEMES);document.documentElement.classList.add(t);localStorage.setItem('ft-theme',t);document.querySelectorAll('.theme-bar button').forEach(b=>b.classList.toggle('active',b===btn));}}

// Set the active theme button on load to match the html class
(function(){{
  var cls=document.documentElement.className;
  document.querySelectorAll('.theme-bar button').forEach(function(b){{
    var t=(b.getAttribute('onclick')||'').match(/'(theme-\w+)'/);
    if(t&&t[1]===cls)b.classList.add('active');else b.classList.remove('active');
  }});
}})();

// Tab switching
document.querySelectorAll('.tab').forEach(function(btn){{
  btn.addEventListener('click',function(){{
    document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    var target=btn.getAttribute('data-target');
    document.querySelectorAll('.panel').forEach(function(p){{
      if(p.id===target){{p.removeAttribute('hidden');p.classList.add('active');}}
      else{{p.setAttribute('hidden','');p.classList.remove('active');}}
    }});
  }});
}});
</script>
</body>
</html>
"""


# ── Status JSON for the workspace portal ──────────────────────────────────────

def render_status_json(failures: list[Failure], summary: dict, report_date: str, report_url: str) -> dict:
    """Compact JSON for the Quintus portal's Tradeflow tab."""
    return {
        "report_date":    report_date,
        "report_url":     report_url,
        "generated_at":   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "failure_count":  len(failures),
        "loss_count":     sum(1 for f in failures if f.kind == "loss"),
        "error_count":    sum(1 for f in failures if f.kind == "exec_error"),
        "summary_7day":   summary,
        "top_failures":   [
            {
                "symbol":   f.symbol,
                "time_est": f.time_est,
                "kind":     f.kind,
                "headline": (
                    f"${f.realized_pl:+,.0f} on {f.quantity}" if f.kind == "loss" and f.realized_pl is not None
                    else (f.rejection_reason or "error")[:80]
                ),
            }
            for f in failures[:5]
        ],
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal",     required=True, help="Path to trades.jsonl")
    parser.add_argument("--output-dir",  required=True, help="Directory to write index.html into")
    parser.add_argument("--status-path", required=True, help="Path to write tradeflow_status.json")
    parser.add_argument("--report-url",  default="https://qlategan-stack.github.io/flowtrader-dashboard/trade-analysis/", help="Public URL of the report")
    parser.add_argument("--no-claude",   action="store_true", help="Skip Claude lesson generation")
    parser.add_argument("--report-date", default=None, help="Override report date (YYYY-MM-DD); default is the latest in the journal")
    args = parser.parse_args()

    journal_path = Path(args.journal)
    output_dir   = Path(args.output_dir)
    status_path  = Path(args.status_path)

    entries = load_jsonl(journal_path)
    if not entries:
        logger.warning(f"No entries found in {journal_path}")
        return 1

    # Prefer most recent date with actual failures so the report is useful.
    # Fall back to latest trading date if there are no failures at all.
    report_date = (
        args.report_date
        or most_recent_failure_date(entries)
        or latest_trading_date(entries)
    )
    if not report_date:
        logger.warning("No actionable entries found in journal")
        return 1

    failures = find_failures(entries, report_date)
    summary  = seven_day_summary(entries, report_date)

    # Claude lessons
    if not args.no_claude and failures:
        try:
            from anthropic import Anthropic
            client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
            for f in failures:
                f.lesson = generate_lesson(f, client)
        except Exception as e:
            logger.warning(f"Claude unavailable, skipping lessons: {e}")

    html = render_html(failures, summary, report_date)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "index.html").write_text(html, encoding="utf-8")
    logger.info(f"Wrote {output_dir / 'index.html'}")

    status = render_status_json(failures, summary, report_date, args.report_url)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
    logger.info(f"Wrote {status_path}")

    print(f"Report date: {report_date}")
    print(f"Failures: {len(failures)} ({summary['losses']} losses, {summary['exec_errors']} errors in 7-day window)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
