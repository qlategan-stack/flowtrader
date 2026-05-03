"""
dashboard.py — FlowTrader Dashboard
Run locally:  streamlit run dashboard.py
Hosted:       deploy to share.streamlit.io (connects to this GitHub repo)

Auto-refreshes every 60 seconds. Market data cached for 60 s,
journal cached for 30 s. No manual intervention needed.
"""

import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
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
</style>
""", unsafe_allow_html=True)

# ── Config ────────────────────────────────────────────────────────────────────
with open("config.yaml") as f:
    CFG = yaml.safe_load(f)
WATCHLIST   = CFG.get("watchlist", {}).get("equities", ["SPY", "QQQ"])
REFRESH_SEC = 60   # how often the page auto-reloads

# ── Cached data — TTLs drive how fresh each panel is ─────────────────────────
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

# ── Header ────────────────────────────────────────────────────────────────────
now_str = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
h1, h2, h3 = st.columns([5, 2, 1])
h1.title("📈 FlowTrader")
h2.caption(f"Updated: {now_str}")
if h3.button("⟳ Refresh", use_container_width=True):
    st.cache_data.clear()
    st.rerun()

st.divider()

# ── Tabs ──────────────────────────────────────────────────────────────────────
tab_market, tab_account, tab_journal = st.tabs(["🔍 Market", "💼 Account", "📓 Journal"])


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 1 — MARKET
# ═══════════════════════════════════════════════════════════════════════════════
with tab_market:

    with st.spinner("Fetching market data…"):
        snapshot = fetch_snapshot(tuple(WATCHLIST))

    wl = snapshot.get("watchlist", [])

    if not wl:
        st.warning("No market data. Check your Alpaca API keys.")
        st.stop()

    # ── Summary row ───────────────────────────────────────────────────────────
    tradeable = [s for s in wl if s.get("setup_quality") not in ["SKIP", "NO_DATA"]]
    top       = wl[0] if wl else {}
    trending  = sum(1 for s in wl if s.get("indicators", {}).get("regime") == "TRENDING")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Symbols Scanned",  len(wl))
    c2.metric("Tradeable Setups", len(tradeable))
    c3.metric("Top Signal Score",
              f"{top.get('indicators',{}).get('signal_score',0)}/6",
              top.get("symbol", "—"))
    c4.metric("Trending (skip)", trending, delta_color="inverse")

    st.divider()

    # ── Watchlist table ───────────────────────────────────────────────────────
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
            "VWAP":      ind.get("vwap", 0),
            "ATR":       ind.get("atr", 0),
            "Stop":      ind.get("stop_loss_price", 0),
            "Target":    ind.get("take_profit_price", 0),
            "Sentiment": sent.get("sentiment", "neutral"),
            "Signals":   ", ".join(ind.get("signals_fired", [])) or "none",
        })

    df = pd.DataFrame(rows)

    def _grade_style(v):
        return {
            "A_GRADE": "background-color:#1b4332;color:#40916c",
            "B_GRADE": "background-color:#1b3a4b;color:#4cc9f0",
            "C_GRADE": "background-color:#2d2a1e;color:#ffd60a",
        }.get(v, "color:#555")

    def _score_style(v):
        if v >= 5:   return "color:#00c853;font-weight:bold"
        elif v >= 3: return "color:#ffab00;font-weight:bold"
        return "color:#ff5252"

    def _regime_style(v):
        return "color:#ff5252;font-weight:bold" if v == "TRENDING" else "color:#00c853"

    st.dataframe(
        df.style
          .applymap(_grade_style,  subset=["Grade"])
          .applymap(_score_style,  subset=["Score"])
          .applymap(_regime_style, subset=["Regime"])
          .format({
              "Price":  "${:,.2f}", "VWAP": "${:,.2f}",
              "Stop":   "${:,.2f}", "Target": "${:,.2f}",
              "ATR":    "{:.2f}",   "BB %B": "{:.3f}",
              "RSI":    "{:.1f}",   "ADX":   "{:.1f}",
          }),
        use_container_width=True,
        hide_index=True,
    )

    # ── Per-symbol detail ─────────────────────────────────────────────────────
    st.subheader("Symbol Detail")
    for item in wl:
        ind   = item.get("indicators", {})
        grade = item.get("setup_quality", "SKIP")
        score = ind.get("signal_score", 0)
        icon  = "🟢" if grade in ["A_GRADE", "B_GRADE"] else "🟡" if grade == "C_GRADE" else "🔴"
        label = f"{icon}  {item['symbol']}  —  {score}/6  |  {grade}  |  {ind.get('regime','?')}"

        with st.expander(label):
            d1, d2, d3 = st.columns(3)
            d1.metric("Price",    f"${ind.get('current_price',0):,.2f}")
            d1.metric("RSI",      f"{ind.get('rsi',0):.1f}")
            d1.metric("ADX",      f"{ind.get('adx',0):.1f}")
            bb = ind.get("bollinger", {})
            d2.metric("BB Upper", f"${bb.get('upper',0):,.2f}")
            d2.metric("BB Mid",   f"${bb.get('middle',0):,.2f}")
            d2.metric("BB Lower", f"${bb.get('lower',0):,.2f}")
            d3.metric("VWAP",     f"${ind.get('vwap',0):,.2f}")
            d3.metric("ATR",      f"{ind.get('atr',0):.2f}")
            d3.metric("MA20",     f"${ind.get('ma20',0):,.2f}")

            fired = ind.get("signals_fired", [])
            if fired:
                st.success("Signals: " + "  ·  ".join(fired))
            else:
                st.info("No signals fired")

            for h in item.get("recent_headlines", [])[:3]:
                st.caption(f"**{h.get('source','')}** — {h.get('headline','')}")


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
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Portfolio Value", f"${portfolio:,.2f}")
    c2.metric("Buying Power",    f"${buying_pwr:,.2f}")
    c3.metric("Cash",            f"${cash:,.2f}")
    c4.metric("Day P&L",
              f"${day_pl:+,.2f}",
              f"{day_pl/portfolio*100:+.2f}%" if portfolio else "0%",
              delta_color="normal")

    st.divider()

    # ── Position capacity ─────────────────────────────────────────────────────
    st.subheader("Position Capacity")
    st.progress(open_pos / 3, text=f"{open_pos} / 3 positions used")

    # ── Daily loss limit ──────────────────────────────────────────────────────
    max_loss  = portfolio * 0.02
    loss_used = abs(day_pl) if day_pl < 0 else 0
    loss_frac = min(loss_used / max_loss, 1.0) if max_loss else 0
    icon = "🔴" if loss_frac >= 0.8 else "🟡" if loss_frac >= 0.5 else "🟢"
    st.caption(f"{icon} Daily loss limit — ${loss_used:,.2f} of ${max_loss:,.2f} ({loss_frac:.0%})")
    st.progress(loss_frac)

    st.divider()

    # ── Open positions ────────────────────────────────────────────────────────
    positions = acct.get("positions", [])
    st.subheader(f"Open Positions ({len(positions)})")

    if not positions:
        st.info("No open positions.")
    else:
        pdf = pd.DataFrame(positions)
        pdf["unrealized_plpc"] = pdf["unrealized_plpc"].apply(lambda v: f"{float(v)*100:+.2f}%")
        pdf["unrealized_pl"]   = pdf["unrealized_pl"].apply(lambda v: f"${float(v):+,.2f}")
        pdf["avg_entry"]       = pdf["avg_entry"].apply(lambda v: f"${float(v):,.2f}")
        pdf["current_price"]   = pdf["current_price"].apply(lambda v: f"${float(v):,.2f}")
        pdf.columns = ["Symbol", "Qty", "Avg Entry", "Current Price", "Unrealized P&L", "P&L %"]
        st.dataframe(pdf, use_container_width=True, hide_index=True)

    # ── P&L history chart ─────────────────────────────────────────────────────
    st.divider()
    st.subheader("Day P&L History (last 14 days)")
    hist = fetch_entries(14)
    pl_by_date = {}
    for e in hist:
        d  = e.get("date", "")
        pl = e.get("day_pl_at_decision")
        if d and pl is not None:
            pl_by_date[d] = pl

    if pl_by_date:
        dates = sorted(pl_by_date)
        vals  = [pl_by_date[d] for d in dates]
        fig   = go.Figure(go.Bar(
            x=dates, y=vals,
            marker_color=["#00c853" if v >= 0 else "#ff5252" for v in vals],
            marker_line_width=0,
        ))
        fig.update_layout(
            plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
            font_color="#fafafa", height=240,
            margin=dict(l=0, r=0, t=4, b=0),
            xaxis=dict(showgrid=False),
            yaxis=dict(gridcolor="#1c1f26", tickprefix="$"),
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No journal data yet — run `python main.py full` to populate history.")


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
    f3.caption(f"{len(filtered)} of {len(all_entries)} entries")

    if not filtered:
        st.info("No journal entries yet. Run `python main.py full` to generate entries.")
    else:
        # ── Summary metrics ───────────────────────────────────────────────────
        trades    = [e for e in filtered if e.get("action") in ["BUY", "SELL"]]
        skips     = [e for e in filtered if e.get("action") == "SKIP"]
        avg_score = sum(e.get("signal_score", 0) for e in filtered) / len(filtered)

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total Cycles",    len(filtered))
        m2.metric("Trades Placed",   len(trades))
        m3.metric("Skips",           len(skips))
        m4.metric("Avg Signal Score",f"{avg_score:.1f}/6")

        st.divider()

        # ── Journal table ─────────────────────────────────────────────────────
        rows = []
        for e in reversed(filtered):
            rows.append({
                "Date":       e.get("date", ""),
                "Time":       e.get("time_est", ""),
                "Action":     e.get("action", ""),
                "Symbol":     e.get("symbol") or "—",
                "Score":      e.get("signal_score", 0),
                "Confidence": e.get("confidence", "—"),
                "Entry":      f"${e['entry_price']:,.2f}" if e.get("entry_price") else "—",
                "Stop":       f"${e['stop_loss']:,.2f}"   if e.get("stop_loss")   else "—",
                "Target":     f"${e['take_profit']:,.2f}" if e.get("take_profit") else "—",
                "R:R":        e.get("risk_reward") or "—",
                "Exec":       e.get("execution_status", "—"),
                "Mode":       "Paper" if e.get("paper_trade", True) else "Live",
            })

        jdf = pd.DataFrame(rows)

        def _action_style(v):
            return {
                "BUY":  "background-color:#1b4332;color:#40916c;font-weight:bold",
                "SELL": "background-color:#3b1f2b;color:#f72585;font-weight:bold",
                "SKIP": "color:#555",
                "HOLD": "color:#ffab00",
            }.get(v, "")

        def _exec_style(v):
            return {
                "FILLED":    "color:#00c853",
                "SUBMITTED": "color:#4cc9f0",
                "REJECTED":  "color:#ff5252",
                "SKIPPED":   "color:#555",
                "SIMULATED": "color:#ffab00",
            }.get(v, "")

        st.dataframe(
            jdf.style
               .applymap(_action_style, subset=["Action"])
               .applymap(_exec_style,   subset=["Exec"]),
            use_container_width=True,
            hide_index=True,
        )

        # ── Entry inspector ───────────────────────────────────────────────────
        st.subheader("Entry Detail")
        rev = list(reversed(filtered))
        labels = [
            f"{e.get('date')} {e.get('time_est')} — {e.get('action')} {e.get('symbol') or ''}"
            for e in rev
        ]
        idx = st.selectbox("Select entry", range(len(labels)),
                           format_func=lambda i: labels[i])
        sel = rev[idx]

        col_a, col_b = st.columns(2)
        with col_a:
            st.markdown("**Decision**")
            st.json({k: sel[k] for k in [
                "action", "symbol", "signal_score", "signals_fired",
                "confidence", "entry_price", "stop_loss", "take_profit",
                "quantity", "risk_reward", "execution_status", "rejection_reason"
            ] if k in sel})
        with col_b:
            st.markdown("**Claude's Reasoning**")
            st.text_area("reasoning", value=sel.get("reasoning", "No reasoning recorded."),
                         height=200, disabled=True, label_visibility="collapsed")
            st.markdown("**Journal Entry**")
            st.text_area("journal", value=sel.get("journal_entry", ""),
                         height=140, disabled=True, label_visibility="collapsed")


# ── Continuous auto-refresh ───────────────────────────────────────────────────
# Shows a live countdown in the footer; reloads the full page when it hits zero.
st.divider()
footer_left, footer_right = st.columns([4, 1])
footer_left.caption("FlowTrader v1  ·  Paper trading mode  ·  Auto-refreshes every 60 s")
countdown_slot = footer_right.empty()

if "next_refresh" not in st.session_state:
    st.session_state.next_refresh = time.time() + REFRESH_SEC

remaining = int(st.session_state.next_refresh - time.time())

if remaining <= 0:
    st.session_state.next_refresh = time.time() + REFRESH_SEC
    st.cache_data.clear()
    st.rerun()
else:
    countdown_slot.caption(f"Next refresh in {remaining}s")
    time.sleep(1)
    st.rerun()
