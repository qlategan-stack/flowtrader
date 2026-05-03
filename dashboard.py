"""
dashboard.py — FlowTrader Dashboard
Run with: streamlit run dashboard.py
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

# Load from .env locally; on Streamlit Cloud secrets are injected as env vars
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
    page_title="FlowTrader Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
    .main { background-color: #0e1117; }
    .metric-card {
        background: #1c1f26;
        border-radius: 8px;
        padding: 16px 20px;
        margin: 4px 0;
    }
    .stTabs [data-baseweb="tab"] { font-size: 16px; font-weight: 600; }
    .signal-high { color: #00c853; font-weight: bold; }
    .signal-med  { color: #ffab00; font-weight: bold; }
    .signal-low  { color: #ff5252; font-weight: bold; }
</style>
""", unsafe_allow_html=True)

# ── Cached data fetchers ──────────────────────────────────────────────────────
@st.cache_resource
def get_fetcher():
    return MarketDataFetcher()

@st.cache_resource
def get_journal():
    return TradeJournal()

@st.cache_data(ttl=60)
def fetch_account():
    return get_fetcher().get_account_snapshot()

@st.cache_data(ttl=60)
def fetch_snapshot(watchlist: tuple):
    return get_fetcher().build_market_snapshot(list(watchlist))

@st.cache_data(ttl=30)
def fetch_journal_entries(days: int):
    return get_journal().get_entries(days=days)

# ── Watchlist from config ─────────────────────────────────────────────────────
import yaml
with open("config.yaml") as f:
    _cfg = yaml.safe_load(f)
WATCHLIST = _cfg.get("watchlist", {}).get("equities", ["SPY", "QQQ"])

# ── Header ────────────────────────────────────────────────────────────────────
col_title, col_time, col_refresh = st.columns([4, 2, 1])
with col_title:
    st.title("📈 FlowTrader Dashboard")
with col_time:
    st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
with col_refresh:
    if st.button("⟳ Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

st.divider()

# ── Tabs ──────────────────────────────────────────────────────────────────────
tab_market, tab_account, tab_journal = st.tabs(["🔍 Market", "💼 Account", "📓 Journal"])


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 1 — MARKET
# ═══════════════════════════════════════════════════════════════════════════════
with tab_market:
    st.subheader("Watchlist Scan")

    with st.spinner("Fetching market data..."):
        snapshot = fetch_snapshot(tuple(WATCHLIST))

    watchlist_data = snapshot.get("watchlist", [])

    if not watchlist_data:
        st.warning("No market data available. Check your Alpaca API keys.")
    else:
        # ── Summary signal cards ──────────────────────────────────────────────
        tradeable = [s for s in watchlist_data if s.get("setup_quality") not in ["SKIP", "NO_DATA"]]
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Symbols Scanned", len(watchlist_data))
        col2.metric("Tradeable Setups", len(tradeable))
        top = watchlist_data[0] if watchlist_data else {}
        top_score = top.get("indicators", {}).get("signal_score", 0)
        col3.metric("Top Signal Score", f"{top_score}/6", top.get("symbol", "—"))
        trending = sum(1 for s in watchlist_data if s.get("indicators", {}).get("regime") == "TRENDING")
        col4.metric("Trending (skip)", trending, delta_color="inverse")

        st.divider()

        # ── Watchlist table ───────────────────────────────────────────────────
        rows = []
        for item in watchlist_data:
            ind = item.get("indicators", {})
            sentiment = item.get("news_sentiment", {})
            rows.append({
                "Symbol":        item["symbol"],
                "Grade":         item.get("setup_quality", "—"),
                "Score":         ind.get("signal_score", 0),
                "Price":         ind.get("current_price", 0),
                "RSI":           ind.get("rsi", "—"),
                "ADX":           ind.get("adx", "—"),
                "Regime":        ind.get("regime", "—"),
                "BB %B":         ind.get("bollinger", {}).get("pct_b", "—"),
                "VWAP":          ind.get("vwap", "—"),
                "ATR":           ind.get("atr", "—"),
                "Stop":          ind.get("stop_loss_price", "—"),
                "Target":        ind.get("take_profit_price", "—"),
                "Sentiment":     sentiment.get("sentiment", "neutral"),
                "Signals":       ", ".join(ind.get("signals_fired", [])) or "none",
            })

        df = pd.DataFrame(rows)

        def colour_grade(val):
            colours = {
                "A_GRADE": "background-color: #1b4332; color: #40916c",
                "B_GRADE": "background-color: #1b3a4b; color: #4cc9f0",
                "C_GRADE": "background-color: #2d2a1e; color: #ffd60a",
                "SKIP":    "background-color: #1a1a1a; color: #6c757d",
                "NO_DATA": "background-color: #1a1a1a; color: #6c757d",
            }
            return colours.get(val, "")

        def colour_score(val):
            if val >= 5:   return "color: #00c853; font-weight: bold"
            elif val >= 3: return "color: #ffab00; font-weight: bold"
            else:          return "color: #ff5252"

        def colour_regime(val):
            return "color: #ff5252; font-weight: bold" if val == "TRENDING" else "color: #00c853"

        styled = (
            df.style
            .applymap(colour_grade, subset=["Grade"])
            .applymap(colour_score, subset=["Score"])
            .applymap(colour_regime, subset=["Regime"])
            .format({
                "Price": "${:,.2f}",
                "VWAP":  lambda v: f"${v:,.2f}" if isinstance(v, (int, float)) else v,
                "Stop":  lambda v: f"${v:,.2f}" if isinstance(v, (int, float)) else v,
                "Target": lambda v: f"${v:,.2f}" if isinstance(v, (int, float)) else v,
                "ATR":   lambda v: f"{v:.2f}" if isinstance(v, (int, float)) else v,
                "BB %B": lambda v: f"{v:.3f}" if isinstance(v, (int, float)) else v,
            })
        )
        st.dataframe(styled, use_container_width=True, hide_index=True)

        # ── Detailed symbol expanders ─────────────────────────────────────────
        st.subheader("Symbol Detail")
        for item in watchlist_data:
            ind = item.get("indicators", {})
            grade = item.get("setup_quality", "SKIP")
            score = ind.get("signal_score", 0)
            label = f"{'🟢' if grade in ['A_GRADE','B_GRADE'] else '🟡' if grade == 'C_GRADE' else '🔴'}  {item['symbol']}  —  Score {score}/6  |  {grade}  |  {ind.get('regime','?')}"

            with st.expander(label):
                c1, c2, c3 = st.columns(3)
                c1.metric("Price",  f"${ind.get('current_price',0):,.2f}")
                c1.metric("RSI",    ind.get("rsi", "—"))
                c1.metric("ADX",    ind.get("adx", "—"))
                c2.metric("BB Upper", f"${ind.get('bollinger',{}).get('upper',0):,.2f}")
                c2.metric("BB Mid",   f"${ind.get('bollinger',{}).get('middle',0):,.2f}")
                c2.metric("BB Lower", f"${ind.get('bollinger',{}).get('lower',0):,.2f}")
                c3.metric("VWAP",   f"${ind.get('vwap',0):,.2f}")
                c3.metric("ATR",    ind.get("atr","—"))
                c3.metric("MA20",   f"${ind.get('ma20',0):,.2f}")

                if ind.get("signals_fired"):
                    st.success("Signals fired: " + " · ".join(ind["signals_fired"]))
                else:
                    st.info("No signals fired")

                headlines = item.get("recent_headlines", [])
                if headlines:
                    st.caption("Recent headlines")
                    for h in headlines[:3]:
                        st.markdown(f"- **{h.get('source','')}** — {h.get('headline','')}")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 2 — ACCOUNT
# ═══════════════════════════════════════════════════════════════════════════════
with tab_account:
    st.subheader("Account Overview")

    with st.spinner("Fetching account data..."):
        account = fetch_account()

    if "error" in account:
        st.error(f"Could not load account: {account['error']}")
    else:
        # ── Key metrics ───────────────────────────────────────────────────────
        c1, c2, c3, c4 = st.columns(4)
        portfolio = account.get("portfolio_value", 0)
        buying_power = account.get("buying_power", 0)
        cash = account.get("cash", 0)
        day_pl = account.get("day_pl", 0)
        open_pos = account.get("open_positions", 0)

        c1.metric("Portfolio Value",  f"${portfolio:,.2f}")
        c2.metric("Buying Power",     f"${buying_power:,.2f}")
        c3.metric("Cash",             f"${cash:,.2f}")
        c4.metric("Day P&L",
                  f"${day_pl:+,.2f}",
                  f"{day_pl/portfolio*100:+.2f}%" if portfolio else "0%",
                  delta_color="normal")

        st.divider()

        # ── Position capacity bar ─────────────────────────────────────────────
        st.subheader("Position Capacity")
        used = open_pos
        max_pos = 3
        st.progress(used / max_pos, text=f"{used} / {max_pos} positions used")

        # ── Daily loss limit bar ──────────────────────────────────────────────
        max_loss = portfolio * 0.02
        loss_used = abs(day_pl) if day_pl < 0 else 0
        loss_pct = min(loss_used / max_loss, 1.0) if max_loss else 0
        colour_label = "🔴" if loss_pct >= 0.8 else "🟡" if loss_pct >= 0.5 else "🟢"
        st.caption(f"{colour_label} Daily loss limit: ${loss_used:,.2f} of ${max_loss:,.2f} used ({loss_pct:.0%})")
        st.progress(loss_pct)

        st.divider()

        # ── Open positions table ──────────────────────────────────────────────
        positions = account.get("positions", [])
        st.subheader(f"Open Positions ({len(positions)})")

        if not positions:
            st.info("No open positions.")
        else:
            pos_df = pd.DataFrame(positions)
            pos_df["unrealized_plpc"] = pos_df["unrealized_plpc"].apply(lambda v: f"{float(v)*100:+.2f}%")
            pos_df["unrealized_pl"]   = pos_df["unrealized_pl"].apply(lambda v: f"${float(v):+,.2f}")
            pos_df["avg_entry"]       = pos_df["avg_entry"].apply(lambda v: f"${float(v):,.2f}")
            pos_df["current_price"]   = pos_df["current_price"].apply(lambda v: f"${float(v):,.2f}")
            pos_df.columns = ["Symbol", "Qty", "Avg Entry", "Current Price", "Unrealized P&L", "P&L %"]
            st.dataframe(pos_df, use_container_width=True, hide_index=True)

        # ── P&L sparkline from journal ────────────────────────────────────────
        st.divider()
        st.subheader("Day P&L History (last 14 days)")
        entries = fetch_journal_entries(14)
        if entries:
            pl_data = {}
            for e in entries:
                date = e.get("date", "")
                pl = e.get("day_pl_at_decision")
                if date and pl is not None:
                    pl_data[date] = pl

            if pl_data:
                pl_df = pd.DataFrame(list(pl_data.items()), columns=["Date", "Day P&L"])
                pl_df = pl_df.sort_values("Date")
                fig = go.Figure()
                fig.add_trace(go.Bar(
                    x=pl_df["Date"],
                    y=pl_df["Day P&L"],
                    marker_color=["#00c853" if v >= 0 else "#ff5252" for v in pl_df["Day P&L"]],
                ))
                fig.update_layout(
                    plot_bgcolor="#0e1117",
                    paper_bgcolor="#0e1117",
                    font_color="#fafafa",
                    margin=dict(l=0, r=0, t=10, b=0),
                    height=250,
                    xaxis=dict(showgrid=False),
                    yaxis=dict(showgrid=True, gridcolor="#1c1f26", tickprefix="$"),
                )
                st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No journal data yet — run the bot to populate history.")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 3 — JOURNAL
# ═══════════════════════════════════════════════════════════════════════════════
with tab_journal:
    st.subheader("Trade Journal")

    # ── Controls ──────────────────────────────────────────────────────────────
    fc1, fc2, fc3 = st.columns([1, 1, 2])
    days_back = fc1.selectbox("Period", [7, 14, 30, 90], index=0, format_func=lambda d: f"Last {d} days")
    action_filter = fc2.multiselect("Action", ["BUY", "SELL", "SKIP", "HOLD"], default=["BUY", "SELL", "SKIP"])

    entries = fetch_journal_entries(days_back)

    if not entries:
        st.info("No journal entries yet. Run `python main.py full` to generate entries.")
    else:
        # Apply filters
        filtered = [e for e in entries if e.get("action") in action_filter]
        fc3.caption(f"{len(filtered)} entries shown of {len(entries)} total")

        # ── Summary metrics ───────────────────────────────────────────────────
        trades = [e for e in filtered if e.get("action") in ["BUY", "SELL"]]
        skips  = [e for e in filtered if e.get("action") == "SKIP"]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total Cycles", len(filtered))
        m2.metric("Trades Placed", len(trades))
        m3.metric("Skips", len(skips))
        avg_score = sum(e.get("signal_score", 0) for e in filtered) / len(filtered) if filtered else 0
        m4.metric("Avg Signal Score", f"{avg_score:.1f}/6")

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
                "Paper":      "📄" if e.get("paper_trade", True) else "💰",
            })

        jdf = pd.DataFrame(rows)

        def colour_action(val):
            return {
                "BUY":  "background-color: #1b4332; color: #40916c; font-weight: bold",
                "SELL": "background-color: #3b1f2b; color: #f72585; font-weight: bold",
                "SKIP": "color: #6c757d",
                "HOLD": "color: #ffab00",
            }.get(val, "")

        def colour_exec(val):
            return {
                "FILLED":    "color: #00c853",
                "SUBMITTED": "color: #4cc9f0",
                "REJECTED":  "color: #ff5252",
                "SKIPPED":   "color: #6c757d",
                "SIMULATED": "color: #ffab00",
            }.get(val, "")

        st.dataframe(
            jdf.style
               .applymap(colour_action, subset=["Action"])
               .applymap(colour_exec,   subset=["Exec"]),
            use_container_width=True,
            hide_index=True,
        )

        # ── Entry detail expander ─────────────────────────────────────────────
        st.subheader("Entry Detail")
        if filtered:
            entry_labels = [
                f"{e.get('date')} {e.get('time_est')} — {e.get('action')} {e.get('symbol') or ''}"
                for e in reversed(filtered)
            ]
            selected_idx = st.selectbox("Select entry to inspect", range(len(entry_labels)),
                                        format_func=lambda i: entry_labels[i])
            selected = list(reversed(filtered))[selected_idx]

            col_a, col_b = st.columns(2)
            with col_a:
                st.markdown("**Decision**")
                st.json({k: v for k, v in selected.items()
                         if k in ["action","symbol","signal_score","signals_fired",
                                  "confidence","entry_price","stop_loss","take_profit",
                                  "quantity","risk_reward","execution_status","rejection_reason"]})
            with col_b:
                st.markdown("**Claude's Reasoning**")
                st.text_area("", value=selected.get("reasoning", "No reasoning recorded."),
                             height=200, disabled=True, label_visibility="collapsed")
                st.markdown("**Journal Entry**")
                st.text_area("", value=selected.get("journal_entry", ""),
                             height=150, disabled=True, label_visibility="collapsed")

# ── Auto-refresh footer ───────────────────────────────────────────────────────
st.divider()
st.caption("Auto-refreshes every 60 seconds · Paper trading mode · FlowTrader v1")

if "last_refresh" not in st.session_state:
    st.session_state.last_refresh = time.time()

if time.time() - st.session_state.last_refresh > 60:
    st.session_state.last_refresh = time.time()
    st.cache_data.clear()
    st.rerun()
