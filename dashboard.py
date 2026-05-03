"""
dashboard.py — FlowTrader Dashboard
Run locally:  streamlit run dashboard.py
Hosted:       deploy to share.streamlit.io (connects to this GitHub repo)

Auto-refreshes every 60 seconds. Market data cached for 60 s,
journal cached for 30 s. No manual intervention needed.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import yaml
from dotenv import load_dotenv

# ── Secrets: .env locally, st.secrets on Streamlit Cloud ─────────────────────
load_dotenv()
try:
    for _k, _v in st.secrets.items():
        os.environ.setdefault(_k, str(_v))
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).parent))
from data.fetcher import MarketDataFetcher
from journal.logger import TradeJournal

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="FlowTrader",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
    .stTabs [data-baseweb="tab"] { font-size: 15px; font-weight: 600; }
    .stTabs [data-baseweb="tab-list"] { gap: 8px; }
    div[data-testid="stMetricValue"] { font-size: 1.4rem; }
    .badge-paper  { background:#1b3a4b; color:#4cc9f0; padding:3px 10px;
                    border-radius:12px; font-size:0.8rem; font-weight:600; }
    .badge-live   { background:#3b1f2b; color:#f72585; padding:3px 10px;
                    border-radius:12px; font-size:0.8rem; font-weight:600; }
    .badge-a      { background:#1b4332; color:#40916c; padding:2px 8px;
                    border-radius:8px; font-weight:700; }
    .badge-b      { background:#1b3a4b; color:#4cc9f0; padding:2px 8px;
                    border-radius:8px; font-weight:700; }
    .badge-c      { background:#2d2a1e; color:#ffd60a; padding:2px 8px;
                    border-radius:8px; font-weight:700; }
    .badge-skip   { color:#555; }
</style>
""", unsafe_allow_html=True)

# ── Config ────────────────────────────────────────────────────────────────────
with open("config.yaml") as f:
    CFG = yaml.safe_load(f)
WATCHLIST    = CFG.get("watchlist", {}).get("equities", ["SPY", "QQQ"])
CRYPTO_LIST  = CFG.get("watchlist", {}).get("crypto", [])
REFRESH_SEC  = 60
PAPER_MODE   = os.getenv("PAPER_TRADING", "true").lower() == "true"

MEMO_JSON = Path("journal/weekly_research_memo.json")

# ── Cached data ───────────────────────────────────────────────────────────────
@st.cache_resource
def _fetcher():
    return MarketDataFetcher()

@st.cache_resource
def _journal():
    return TradeJournal()

@st.cache_data(ttl=REFRESH_SEC)
def fetch_account():
    return _fetcher().get_account_snapshot()

@st.cache_data(ttl=REFRESH_SEC)
def fetch_snapshot(watchlist: tuple):
    return _fetcher().build_market_snapshot(list(watchlist))

@st.cache_data(ttl=30)
def fetch_entries(days: int):
    return _journal().get_entries(days=days)

@st.cache_data(ttl=300)
def load_research_memo() -> dict:
    if not MEMO_JSON.exists():
        return {}
    try:
        return json.loads(MEMO_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}

# ── Agent runner ─────────────────────────────────────────────────────────────
def _stream_agent(mode: str, timeout: int = 300, label: str = "") -> tuple[int, str, str]:
    """
    Run `python main.py <mode>` via Popen and stream stdout line-by-line into
    three Streamlit placeholder widgets:
      • a status line   (st.empty → caption)
      • a progress bar  (st.progress)
      • a scrolling log (st.empty → code block, last 30 lines)

    Returns (returncode, full_stdout, full_stderr) so callers can inspect output.
    """
    cwd = str(Path(__file__).parent)

    status_ph = st.empty()
    bar_ph    = st.progress(0, text=f"Starting {label or mode}…")
    log_ph    = st.empty()

    stdout_lines: list[str] = []
    stderr_buf:   list[str] = []
    start = time.time()

    status_ph.caption(f"⏳ Running `{mode}` — started at {datetime.now().strftime('%H:%M:%S')}")

    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", "main.py", mode],  # -u = unbuffered stdout
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env={**os.environ},
        )
    except Exception as exc:
        bar_ph.empty()
        status_ph.error(f"Failed to start process: {exc}")
        return -1, "", str(exc)

    def _refresh_log():
        tail = stdout_lines[-30:] if stdout_lines else ["(waiting for output…)"]
        log_ph.code("\n".join(tail), language="text")

    def _update_bar(fraction: float, text: str):
        bar_ph.progress(min(fraction, 1.0), text=text)

    elapsed = 0.0
    try:
        while True:
            line = proc.stdout.readline()
            if line:
                stdout_lines.append(line.rstrip())
                elapsed = time.time() - start
                frac = min(elapsed / timeout, 0.95)
                _update_bar(frac, f"Running… {int(elapsed)}s elapsed")
                _refresh_log()
            elif proc.poll() is not None:
                break

            if time.time() - start > timeout:
                proc.kill()
                stderr_buf.append(f"Timed out after {timeout}s")
                break
    finally:
        try:
            remaining_err = proc.stderr.read()
            if remaining_err:
                stderr_buf.append(remaining_err)
        except Exception:
            pass

    rc = proc.returncode if proc.returncode is not None else -1
    elapsed = time.time() - start

    _refresh_log()
    if rc == 0:
        bar_ph.progress(1.0, text=f"Done in {int(elapsed)}s")
        status_ph.success(f"✅ `{mode}` completed in {int(elapsed)}s")
    else:
        bar_ph.empty()
        status_ph.error(f"❌ `{mode}` exited with code {rc} after {int(elapsed)}s")

    return rc, "\n".join(stdout_lines), "\n".join(stderr_buf)


# ── Sidebar — Control Panel ───────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚙️ Control Panel")
    st.caption(f"{'🟡 PAPER' if PAPER_MODE else '🔴 LIVE'} mode active")
    st.divider()

    # ── Data refresh ──────────────────────────────────────────────────────────
    st.markdown("**Data**")
    if st.button("⟳ Refresh All Data", use_container_width=True, help="Clear cache and reload market data, account, and journal"):
        st.cache_data.clear()
        st.session_state.next_refresh = time.time() + REFRESH_SEC
        st.rerun()

    if st.button("⟳ Refresh Market Only", use_container_width=True, help="Re-fetch watchlist prices and indicators"):
        fetch_snapshot.clear()
        st.rerun()

    if st.button("⟳ Refresh Account Only", use_container_width=True, help="Re-fetch account balance and positions"):
        fetch_account.clear()
        st.rerun()

    st.divider()

    # ── Run agents ────────────────────────────────────────────────────────────
    st.markdown("**Run Agents**")
    st.caption("Each button calls `main.py` directly. Output is shown below after completion.")

    run_scan   = st.button("🔍 Run Market Scan",       use_container_width=True,
                           help="Fetch latest data, get Claude's decision, execute if signal qualifies (test mode — no order placed)")
    run_test   = st.button("🧪 Run Test (no trade)",   use_container_width=True,
                           help="Fetch account snapshot only — no market scan, no trade")
    run_review = st.button("📋 Run Weekly Review",     use_container_width=True,
                           help="Ask Claude to review the last 7 days of journal entries and write a summary")
    run_research = st.button("🧠 Run Research Analyst", use_container_width=True,
                             help="Run the full Sunday research analyst: VIX, sectors, broader scan, earnings calendar, weekly memo")

    st.divider()
    st.markdown("**Auto-refresh**")
    auto_refresh = st.toggle("Auto-refresh every 60 s", value=True,
                             help="Disable while running agents to prevent page interruptions")

    # ── Agent output panel ────────────────────────────────────────────────────
    if run_test:
        st.markdown("**Test Run**")
        code, out, err = _stream_agent("test", timeout=60, label="Test scan")
        if code != 0 and (err or out):
            st.code((err or out)[-2000:], language="text")

    if run_scan:
        st.markdown("**Market Scan**")
        code, out, err = _stream_agent("full", timeout=180, label="Market scan")
        if code == 0:
            fetch_entries.clear()
            fetch_account.clear()
        elif err or out:
            st.code((err or out)[-2000:], language="text")

    if run_review:
        st.markdown("**Weekly Review**")
        code, out, err = _stream_agent("weekly-review", timeout=180, label="Weekly review")
        if code == 0 and out:
            with st.expander("Review text", expanded=True):
                st.text(out[-3000:] if len(out) > 3000 else out)
        elif err or out:
            st.code((err or out)[-2000:], language="text")

    if run_research:
        st.markdown("**Research Analyst**")
        code, out, err = _stream_agent("research-analyst", timeout=360, label="Research analyst")
        if code == 0:
            load_research_memo.clear()
        elif err or out:
            st.code((err or out)[-2000:], language="text")


# ── Helpers ───────────────────────────────────────────────────────────────────
def _grade_colour(grade: str) -> str:
    return {
        "A_GRADE": "color:#40916c;font-weight:700",
        "B_GRADE": "color:#4cc9f0;font-weight:700",
        "C_GRADE": "color:#ffd60a;font-weight:700",
    }.get(grade, "color:#555")

def _score_colour(v):
    if v >= 5:   return "color:#00c853;font-weight:bold"
    elif v >= 3: return "color:#ffab00;font-weight:bold"
    return "color:#ff5252"

def _regime_colour(v):
    return "color:#ff5252;font-weight:bold" if v == "TRENDING" else "color:#00c853"

def _action_colour(v):
    return {
        "BUY":  "background-color:#1b4332;color:#40916c;font-weight:bold",
        "SELL": "background-color:#3b1f2b;color:#f72585;font-weight:bold",
        "SKIP": "color:#555",
        "HOLD": "color:#ffab00",
    }.get(v, "")

def _exec_colour(v):
    return {
        "FILLED":    "color:#00c853",
        "SUBMITTED": "color:#4cc9f0",
        "REJECTED":  "color:#ff5252",
        "SKIPPED":   "color:#555",
        "SIMULATED": "color:#ffab00",
    }.get(v, "")

def _sentiment_colour(v):
    return {"positive": "color:#00c853", "negative": "color:#ff5252"}.get(v, "color:#888")

def _dark_bar(vals, dates, yprefix="$", height=220):
    fig = go.Figure(go.Bar(
        x=dates, y=vals,
        marker_color=["#00c853" if v >= 0 else "#ff5252" for v in vals],
        marker_line_width=0,
    ))
    fig.update_layout(
        plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
        font_color="#fafafa", height=height,
        margin=dict(l=0, r=0, t=4, b=0),
        xaxis=dict(showgrid=False),
        yaxis=dict(gridcolor="#1c1f26", tickprefix=yprefix),
    )
    return fig

# ── Header ────────────────────────────────────────────────────────────────────
now_str = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
mode_badge = '<span class="badge-paper">PAPER</span>' if PAPER_MODE else '<span class="badge-live">LIVE</span>'

h1, h2, h3 = st.columns([5, 2, 1])
h1.markdown(f"## 📈 FlowTrader  {mode_badge}", unsafe_allow_html=True)
h2.caption(f"Updated: {now_str}")
if h3.button("⟳ Refresh", use_container_width=True):
    st.cache_data.clear()
    st.rerun()

st.divider()

# ── Tabs ──────────────────────────────────────────────────────────────────────
tab_market, tab_account, tab_journal, tab_research = st.tabs([
    "🔍 Market", "💼 Account", "📓 Journal", "🧠 Research"
])


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 1 — MARKET
# ═══════════════════════════════════════════════════════════════════════════════
with tab_market:

    with st.spinner("Fetching market data…"):
        snapshot = fetch_snapshot(tuple(WATCHLIST))

    wl = snapshot.get("watchlist", [])

    if not wl:
        st.warning("No market data — check your Alpaca API keys.")
        st.stop()

    # ── Summary metrics ───────────────────────────────────────────────────────
    tradeable = [s for s in wl if s.get("setup_quality") not in ["SKIP", "NO_DATA"]]
    top       = wl[0] if wl else {}
    trending  = sum(1 for s in wl if s.get("indicators", {}).get("regime") == "TRENDING")
    a_grades  = sum(1 for s in wl if s.get("setup_quality") == "A_GRADE")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Symbols Scanned",   len(wl))
    c2.metric("Tradeable Setups",  len(tradeable))
    c3.metric("A-Grade Setups",    a_grades)
    c4.metric("Top Signal Score",
              f"{top.get('indicators',{}).get('signal_score',0)}/6",
              top.get("symbol", "—"))
    c5.metric("Trending (skip)",   trending, delta_color="inverse")

    st.divider()

    # ── Watchlist table ───────────────────────────────────────────────────────
    st.subheader("Equities Watchlist")
    rows = []
    for item in wl:
        ind  = item.get("indicators", {})
        sent = item.get("news_sentiment", {})
        rows.append({
            "Symbol":    item["symbol"],
            "Grade":     item.get("setup_quality", "—"),
            "Score":     ind.get("signal_score", 0),
            "Price":     ind.get("current_price", 0),
            "RSI":       ind.get("rsi", 0),
            "ADX":       ind.get("adx", 0),
            "Regime":    ind.get("regime", "—"),
            "BB %B":     ind.get("bollinger", {}).get("pct_b", 0),
            "MA20":      ind.get("ma20", 0),
            "ATR":       ind.get("atr", 0),
            "Stop":      ind.get("stop_loss_price", 0),
            "Target":    ind.get("take_profit_price", 0),
            "Sentiment": sent.get("sentiment", "neutral"),
            "Signals":   ", ".join(ind.get("signals_fired", [])) or "none",
        })

    df = pd.DataFrame(rows)
    st.dataframe(
        df.style
          .map(_grade_colour,   subset=["Grade"])
          .map(_score_colour,   subset=["Score"])
          .map(_regime_colour,  subset=["Regime"])
          .map(_sentiment_colour, subset=["Sentiment"])
          .format({
              "Price":  "${:,.2f}", "MA20": "${:,.2f}",
              "Stop":   "${:,.2f}", "Target": "${:,.2f}",
              "ATR":    "{:.2f}",   "BB %B": "{:.3f}",
              "RSI":    "{:.1f}",   "ADX":   "{:.1f}",
          }),
        use_container_width=True,
        hide_index=True,
    )

    # ── Signal score bar chart ────────────────────────────────────────────────
    if rows:
        score_fig = go.Figure(go.Bar(
            x=[r["Symbol"] for r in rows],
            y=[r["Score"] for r in rows],
            marker_color=["#00c853" if r["Score"] >= 5 else "#ffab00" if r["Score"] >= 3 else "#ff5252" for r in rows],
            marker_line_width=0,
            text=[r["Grade"].replace("_GRADE","") for r in rows],
            textposition="outside",
        ))
        score_fig.update_layout(
            plot_bgcolor="#0e1117", paper_bgcolor="#0e1117", font_color="#fafafa",
            height=200, margin=dict(l=0, r=0, t=8, b=0),
            yaxis=dict(range=[0, 7], gridcolor="#1c1f26", title="Signal Score"),
            xaxis=dict(showgrid=False),
            showlegend=False,
        )
        st.plotly_chart(score_fig, use_container_width=True)

    # ── Per-symbol detail expanders ───────────────────────────────────────────
    st.divider()
    st.subheader("Symbol Detail")
    for item in wl:
        ind   = item.get("indicators", {})
        grade = item.get("setup_quality", "SKIP")
        score = ind.get("signal_score", 0)
        sent  = item.get("news_sentiment", {})
        icon  = "🟢" if grade in ["A_GRADE", "B_GRADE"] else "🟡" if grade == "C_GRADE" else "🔴"
        label = f"{icon}  {item['symbol']}  —  Score {score}/6  |  {grade}  |  {ind.get('regime','?')}"

        with st.expander(label):
            d1, d2, d3, d4 = st.columns(4)

            d1.metric("Price",    f"${ind.get('current_price',0):,.2f}")
            d1.metric("MA20",     f"${ind.get('ma20',0):,.2f}")
            d1.metric("MA50",     f"${ind.get('ma50',0):,.2f}")

            d2.metric("RSI",      f"{ind.get('rsi',0):.1f}")
            d2.metric("ADX",      f"{ind.get('adx',0):.1f}")
            d2.metric("ATR",      f"{ind.get('atr',0):.2f}")

            bb = ind.get("bollinger", {})
            d3.metric("BB Upper", f"${bb.get('upper',0):,.2f}")
            d3.metric("BB Mid",   f"${bb.get('middle',0):,.2f}")
            d3.metric("BB Lower", f"${bb.get('lower',0):,.2f}")

            d4.metric("VWAP",     f"${ind.get('vwap',0):,.2f}")
            d4.metric("Stop",     f"${ind.get('stop_loss_price',0):,.2f}")
            d4.metric("Target",   f"${ind.get('take_profit_price',0):,.2f}")

            fired = ind.get("signals_fired", [])
            if fired:
                st.success("Signals fired:  " + "  ·  ".join(fired))
            else:
                st.info("No signals fired")

            sent_score = sent.get("score", 0)
            sent_label = sent.get("sentiment", "neutral")
            sent_icon  = "📈" if sent_label == "positive" else "📉" if sent_label == "negative" else "➡️"
            st.caption(f"{sent_icon} Sentiment: **{sent_label}** (score {sent_score:+.3f},  {sent.get('article_count', 0)} articles)")

            headlines = item.get("recent_headlines", [])
            if headlines:
                for h in headlines[:3]:
                    pub = h.get("published", "")[:10]
                    st.caption(f"[{pub}] **{h.get('source','')}** — {h.get('headline','')}")

    # ── Crypto watchlist (read-only info) ────────────────────────────────────
    if CRYPTO_LIST:
        st.divider()
        st.subheader("Crypto Watchlist (configured)")
        st.caption("Crypto pairs run 24/7. Live Alpaca crypto data requires a funded account.")
        cc = st.columns(len(CRYPTO_LIST))
        for i, sym in enumerate(CRYPTO_LIST):
            cc[i].info(f"**{sym}**\nMonitored via Alpaca Crypto API")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 2 — ACCOUNT
# ═══════════════════════════════════════════════════════════════════════════════
with tab_account:

    with st.spinner("Fetching account data…"):
        acct = fetch_account()

    if "error" in acct:
        st.error(f"Could not load account: {acct['error']}")
        st.stop()

    portfolio  = float(acct.get("portfolio_value", 0))
    buying_pwr = float(acct.get("buying_power", 0))
    cash       = float(acct.get("cash", 0))
    day_pl     = float(acct.get("day_pl", 0))
    open_pos   = int(acct.get("open_positions", 0))

    # ── Top metrics ───────────────────────────────────────────────────────────
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Portfolio Value",  f"${portfolio:,.2f}")
    c2.metric("Buying Power",     f"${buying_pwr:,.2f}")
    c3.metric("Cash",             f"${cash:,.2f}")
    c4.metric("Day P&L",
              f"${day_pl:+,.2f}",
              f"{day_pl/portfolio*100:+.2f}%" if portfolio else "0%",
              delta_color="normal")
    c5.metric("Invested",
              f"${portfolio - cash:,.2f}",
              f"{(portfolio - cash)/portfolio*100:.1f}% of portfolio" if portfolio else "—")

    st.divider()

    # ── Risk gauges ───────────────────────────────────────────────────────────
    risk_col1, risk_col2 = st.columns(2)

    with risk_col1:
        st.subheader("Position Capacity")
        cap_frac = open_pos / 3
        cap_icon = "🔴" if cap_frac >= 1.0 else "🟡" if cap_frac >= 0.67 else "🟢"
        st.progress(cap_frac, text=f"{cap_icon} {open_pos} / 3 positions used")

    with risk_col2:
        st.subheader("Daily Loss Limit")
        max_loss  = portfolio * 0.02
        loss_used = abs(day_pl) if day_pl < 0 else 0
        loss_frac = min(loss_used / max_loss, 1.0) if max_loss else 0
        loss_icon = "🔴" if loss_frac >= 0.8 else "🟡" if loss_frac >= 0.5 else "🟢"
        st.progress(loss_frac,
                    text=f"{loss_icon} ${loss_used:,.2f} of ${max_loss:,.2f} max ({loss_frac:.0%} used)")

    st.divider()

    # ── Open positions table ──────────────────────────────────────────────────
    positions = acct.get("positions", [])
    st.subheader(f"Open Positions ({len(positions)} / 3)")

    if not positions:
        st.info("No open positions.")
    else:
        prows = []
        for p in positions:
            pl    = float(p.get("unrealized_pl", 0))
            plpct = float(p.get("unrealized_plpc", 0)) * 100
            prows.append({
                "Symbol":        p.get("symbol"),
                "Qty":           float(p.get("qty", 0)),
                "Avg Entry":     float(p.get("avg_entry", 0)),
                "Current Price": float(p.get("current_price", 0)),
                "Unrealized P&L": pl,
                "P&L %":         plpct,
            })
        pdf = pd.DataFrame(prows)

        def _pl_colour(v):
            return "color:#00c853;font-weight:bold" if v >= 0 else "color:#ff5252;font-weight:bold"

        st.dataframe(
            pdf.style
               .map(_pl_colour, subset=["Unrealized P&L", "P&L %"])
               .format({
                   "Avg Entry":      "${:,.2f}",
                   "Current Price":  "${:,.2f}",
                   "Unrealized P&L": "${:+,.2f}",
                   "P&L %":          "{:+.2f}%",
                   "Qty":            "{:,.4f}",
               }),
            use_container_width=True,
            hide_index=True,
        )

    # ── Day P&L history chart ─────────────────────────────────────────────────
    st.divider()
    st.subheader("Day P&L History (last 14 days)")

    hist = fetch_entries(14)
    pl_by_date: dict = {}
    for e in hist:
        d  = e.get("date", "")
        pl = e.get("day_pl_at_decision")
        if d and pl is not None:
            pl_by_date[d] = pl  # last value for the day wins

    if pl_by_date:
        dates = sorted(pl_by_date)
        vals  = [pl_by_date[d] for d in dates]
        st.plotly_chart(_dark_bar(vals, dates, yprefix="$"), use_container_width=True)
    else:
        st.info("No journal data yet — P&L history will populate after the first bot run.")

    # ── Journal performance summary ───────────────────────────────────────────
    st.divider()
    st.subheader("Performance Summary (last 7 days)")

    week_entries = fetch_entries(7)
    if week_entries:
        filled_trades = [e for e in week_entries if e.get("action") in ["BUY","SELL"]
                         and e.get("execution_status") in ["FILLED","SIMULATED","SUBMITTED"]]
        skips         = [e for e in week_entries if e.get("action") == "SKIP"]
        scores        = [e.get("signal_score", 0) for e in week_entries]

        p1, p2, p3, p4 = st.columns(4)
        p1.metric("Total Cycles",   len(week_entries))
        p2.metric("Trades Placed",  len(filled_trades))
        p3.metric("Skips",          len(skips))
        p4.metric("Avg Score",      f"{sum(scores)/len(scores):.1f}/6" if scores else "—")
    else:
        st.info("No journal data for the last 7 days.")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 3 — JOURNAL
# ═══════════════════════════════════════════════════════════════════════════════
with tab_journal:

    # ── Filters ───────────────────────────────────────────────────────────────
    f1, f2, f3 = st.columns([1, 2, 3])
    days_back     = f1.selectbox("Period", [7, 14, 30, 90], format_func=lambda d: f"Last {d} days")
    action_filter = f2.multiselect("Action", ["BUY", "SELL", "SKIP", "HOLD"],
                                   default=["BUY", "SELL", "SKIP"])

    all_entries = fetch_entries(days_back)
    filtered    = [e for e in all_entries if e.get("action") in action_filter]
    f3.caption(f"{len(filtered)} of {len(all_entries)} entries shown")

    if not filtered:
        st.info("No journal entries yet. Run `python main.py full` to generate entries.")
    else:
        # ── Summary metrics ───────────────────────────────────────────────────
        trades    = [e for e in filtered if e.get("action") in ["BUY", "SELL"]]
        skips     = [e for e in filtered if e.get("action") == "SKIP"]
        filled    = [e for e in trades   if e.get("execution_status") in ["FILLED","SIMULATED","SUBMITTED"]]
        avg_score = sum(e.get("signal_score", 0) for e in filtered) / len(filtered)
        skip_rate = len(skips) / len(filtered) * 100 if filtered else 0

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Total Cycles",     len(filtered))
        m2.metric("Trades Placed",    len(trades))
        m3.metric("Orders Executed",  len(filled))
        m4.metric("Skips",            len(skips))
        m5.metric("Skip Rate",        f"{skip_rate:.0f}%")

        st.divider()

        # ── Charts row ────────────────────────────────────────────────────────
        chart_col1, chart_col2 = st.columns(2)

        with chart_col1:
            st.caption("Signal Score Distribution")
            score_counts = {}
            for e in filtered:
                s = e.get("signal_score", 0)
                score_counts[s] = score_counts.get(s, 0) + 1
            if score_counts:
                sc_fig = go.Figure(go.Bar(
                    x=list(score_counts.keys()),
                    y=list(score_counts.values()),
                    marker_color=["#00c853" if k >= 5 else "#ffab00" if k >= 3 else "#ff5252"
                                  for k in score_counts],
                    marker_line_width=0,
                ))
                sc_fig.update_layout(
                    plot_bgcolor="#0e1117", paper_bgcolor="#0e1117", font_color="#fafafa",
                    height=200, margin=dict(l=0, r=0, t=4, b=0),
                    xaxis=dict(title="Score", showgrid=False, tickmode="linear"),
                    yaxis=dict(title="Count", gridcolor="#1c1f26"),
                )
                st.plotly_chart(sc_fig, use_container_width=True)

        with chart_col2:
            st.caption("Trade vs Skip Breakdown")
            label_map  = {"BUY": "Buy", "SELL": "Sell", "SKIP": "Skip", "HOLD": "Hold"}
            pie_counts = {}
            for e in filtered:
                a = label_map.get(e.get("action","SKIP"), "Skip")
                pie_counts[a] = pie_counts.get(a, 0) + 1
            if pie_counts:
                pie_fig = go.Figure(go.Pie(
                    labels=list(pie_counts.keys()),
                    values=list(pie_counts.values()),
                    hole=0.55,
                    marker_colors=["#40916c","#f72585","#555","#ffab00"],
                ))
                pie_fig.update_layout(
                    paper_bgcolor="#0e1117", font_color="#fafafa",
                    height=200, margin=dict(l=0, r=0, t=4, b=0),
                    showlegend=True, legend=dict(orientation="h"),
                )
                st.plotly_chart(pie_fig, use_container_width=True)

        st.divider()

        # ── Journal table ─────────────────────────────────────────────────────
        st.subheader("All Entries")
        rows = []
        for e in reversed(filtered):
            rows.append({
                "Date":       e.get("date", ""),
                "Time":       e.get("time_est", ""),
                "Action":     e.get("action", ""),
                "Symbol":     e.get("symbol") or "—",
                "Score":      e.get("signal_score", 0),
                "Confidence": e.get("confidence", "—"),
                "Entry $":    e.get("entry_price"),
                "Stop $":     e.get("stop_loss"),
                "Target $":   e.get("take_profit"),
                "R:R":        e.get("risk_reward") or "—",
                "Exec":       e.get("execution_status", "—"),
                "Mode":       "Paper" if e.get("paper_trade", True) else "Live",
            })

        jdf = pd.DataFrame(rows)

        def _format_price(v):
            return f"${v:,.2f}" if v is not None else "—"

        st.dataframe(
            jdf.style
               .map(_action_colour, subset=["Action"])
               .map(_exec_colour,   subset=["Exec"])
               .map(_score_colour,  subset=["Score"])
               .format({
                   "Entry $":  _format_price,
                   "Stop $":   _format_price,
                   "Target $": _format_price,
               }),
            use_container_width=True,
            hide_index=True,
        )

        # ── Entry inspector ───────────────────────────────────────────────────
        st.divider()
        st.subheader("Entry Inspector")
        rev    = list(reversed(filtered))
        labels = [
            f"{e.get('date')} {e.get('time_est','').split('.')[0]}  |  "
            f"{e.get('action')} {e.get('symbol') or ''}  |  "
            f"Score {e.get('signal_score',0)}/6  |  {e.get('execution_status','—')}"
            for e in rev
        ]
        idx = st.selectbox("Select entry", range(len(labels)),
                           format_func=lambda i: labels[i])
        sel = rev[idx]

        insp1, insp2 = st.columns(2)

        with insp1:
            st.markdown("**Decision Details**")
            dec_keys = [
                "action","symbol","signal_score","signals_fired",
                "confidence","entry_price","stop_loss","take_profit",
                "quantity","risk_reward","execution_status","rejection_reason"
            ]
            st.json({k: sel[k] for k in dec_keys if k in sel})

            st.markdown("**Market Context at Decision Time**")
            ctx_keys = [
                "account_value","open_positions","day_pl_at_decision",
                "top_setup_symbol","top_setup_score","paper_trade"
            ]
            st.json({k: sel[k] for k in ctx_keys if k in sel})

        with insp2:
            st.markdown("**Claude's Reasoning**")
            st.text_area(
                "reasoning",
                value=sel.get("reasoning", "No reasoning recorded."),
                height=220,
                disabled=True,
                label_visibility="collapsed",
            )
            st.markdown("**Journal Entry**")
            st.text_area(
                "journal",
                value=sel.get("journal_entry", "—"),
                height=160,
                disabled=True,
                label_visibility="collapsed",
            )


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 4 — RESEARCH (Weekly Analyst Memo)
# ═══════════════════════════════════════════════════════════════════════════════
with tab_research:

    memo = load_research_memo()

    if not memo:
        st.info(
            "No research memo found yet.\n\n"
            "The Research Analyst runs automatically **every Sunday at 18:00 EST** via GitHub Actions.\n\n"
            "To run it manually: `python main.py research-analyst`"
        )
    else:
        gen_at   = memo.get("generated_at", "Unknown")
        valid_to = memo.get("valid_until", "Unknown")
        expired  = memo.get("expired", False)

        # ── Memo header ───────────────────────────────────────────────────────
        rh1, rh2 = st.columns([3, 1])
        with rh1:
            st.subheader("Weekly Research Memo")
            st.caption(f"Generated: {gen_at[:19].replace('T',' ')} EST   |   Valid until: {valid_to[:10]}")
        with rh2:
            if expired:
                st.warning("⚠️ Memo expired — run fresh analysis")

        # ── Confidence + Regime ───────────────────────────────────────────────
        confidence = memo.get("confidence_score", 0)
        regime     = memo.get("market_regime", "UNKNOWN")
        reason     = memo.get("confidence_reason", "")

        conf_col, reg_col, reason_col = st.columns([1, 1, 3])

        conf_color = "#00c853" if confidence >= 7 else "#ffab00" if confidence >= 5 else "#ff5252"
        conf_col.metric("Trading Confidence", f"{confidence}/10")

        reg_color = "#ff5252" if "TREND" in regime.upper() else "#00c853"
        reg_col.metric("Market Regime", regime)

        reason_col.info(f"**Rationale:** {reason}")

        # ── Confidence gauge (horizontal bar) ────────────────────────────────
        st.progress(confidence / 10,
                    text=f"Confidence {confidence}/10  —  {'Strong conditions' if confidence >= 7 else 'Moderate conditions' if confidence >= 5 else 'Caution — reduced activity recommended'}")

        st.divider()

        # ── Top 3 Opportunities ───────────────────────────────────────────────
        opportunities = memo.get("top_opportunities", [])
        st.subheader(f"Top {len(opportunities)} Opportunities This Week")

        if opportunities:
            opp_cols = st.columns(min(len(opportunities), 3))
            for i, opp in enumerate(opportunities[:3]):
                with opp_cols[i]:
                    if isinstance(opp, dict):
                        sym    = opp.get("symbol", "?")
                        why    = opp.get("reason", opp.get("why", ""))
                        score  = opp.get("signal_strength", opp.get("score", ""))
                        sizing = opp.get("position_size_note", "")
                        st.markdown(f"### {sym}")
                        st.caption(why[:200] if why else "—")
                        if score:
                            st.caption(f"Signal strength: {score}")
                        if sizing:
                            st.caption(f"Sizing note: {sizing}")
                    else:
                        st.markdown(f"- {str(opp)[:200]}")
        else:
            st.info("No specific opportunities identified this week.")

        st.divider()

        # ── Sector Performance ────────────────────────────────────────────────
        sector_focus = memo.get("sector_focus", {})
        st.subheader("Sector Focus")

        sec_col1, sec_col2 = st.columns(2)
        with sec_col1:
            favour = sector_focus.get("favour", sector_focus.get("best", []))
            st.markdown("**Sectors to Favour**")
            if isinstance(favour, list):
                for s in favour:
                    st.success(f"✅ {s}" if isinstance(s, str) else f"✅ {s.get('sector','?')} — {s.get('reason','')[:80]}")
            elif favour:
                st.success(str(favour))

        with sec_col2:
            avoid_sec = sector_focus.get("avoid", sector_focus.get("worst", []))
            st.markdown("**Sectors to Avoid / Underweight**")
            if isinstance(avoid_sec, list):
                for s in avoid_sec:
                    st.error(f"⛔ {s}" if isinstance(s, str) else f"⛔ {s.get('sector','?')} — {s.get('reason','')[:80]}")
            elif avoid_sec:
                st.error(str(avoid_sec))

        st.divider()

        # ── Watchlist Changes ─────────────────────────────────────────────────
        wl_changes = memo.get("watchlist_changes", {})
        st.subheader("Watchlist Recommendations")

        wc1, wc2, wc3 = st.columns(3)
        with wc1:
            adds = wl_changes.get("add", [])
            st.markdown("**Add to Watchlist**")
            if adds:
                for sym in adds:
                    st.success(f"➕ {sym}")
            else:
                st.caption("No additions recommended")

        with wc2:
            removes = wl_changes.get("remove", [])
            st.markdown("**Remove from Watchlist**")
            if removes:
                for sym in removes:
                    st.warning(f"➖ {sym}")
            else:
                st.caption("No removals recommended")

        with wc3:
            avoid_syms = wl_changes.get("avoid_earnings", wl_changes.get("avoid", []))
            st.markdown("**Avoid (Earnings Risk)**")
            if avoid_syms:
                for sym in avoid_syms:
                    st.error(f"⚠️ {sym}")
            else:
                st.caption("No earnings conflicts this week")

        st.divider()

        # ── Risk Warnings ─────────────────────────────────────────────────────
        risk_warnings = memo.get("risk_warnings", [])
        st.subheader("Risk Warnings")

        if risk_warnings:
            for warn in risk_warnings:
                if isinstance(warn, dict):
                    severity = str(warn.get("severity", "")).upper()
                    text     = warn.get("warning", warn.get("description", str(warn)))
                    if severity in ["HIGH", "CRITICAL"]:
                        st.error(f"🔴 {text}")
                    elif severity == "MEDIUM":
                        st.warning(f"🟡 {text}")
                    else:
                        st.info(f"🔵 {text}")
                else:
                    st.warning(f"⚠️ {str(warn)}")
        else:
            st.success("No significant risk warnings for this week.")

        st.divider()

        # ── Raw analysis (collapsible) ────────────────────────────────────────
        with st.expander("Full Raw Analysis from Claude"):
            raw = memo.get("raw_analysis", "")
            if raw:
                st.markdown(raw)
            else:
                st.info("No raw analysis text available.")

        # ── Full JSON (debug) ─────────────────────────────────────────────────
        with st.expander("Memo JSON (debug)"):
            st.json({k: v for k, v in memo.items() if k != "raw_analysis"})


# ── Continuous auto-refresh ───────────────────────────────────────────────────
st.divider()
footer_left, footer_right = st.columns([4, 1])
footer_left.caption(
    f"FlowTrader v1  ·  {'Paper' if PAPER_MODE else 'Live'} trading  ·  "
    + (f"Auto-refreshes every {REFRESH_SEC} s" if auto_refresh else "Auto-refresh paused — toggle in sidebar to resume")
)
countdown_slot = footer_right.empty()

if "next_refresh" not in st.session_state:
    st.session_state.next_refresh = time.time() + REFRESH_SEC

if auto_refresh:
    remaining = int(st.session_state.next_refresh - time.time())
    if remaining <= 0:
        st.session_state.next_refresh = time.time() + REFRESH_SEC
        st.cache_data.clear()
        st.rerun()
    else:
        countdown_slot.caption(f"Next refresh in {remaining}s")
        time.sleep(1)
        st.rerun()
else:
    countdown_slot.caption("Auto-refresh paused")
