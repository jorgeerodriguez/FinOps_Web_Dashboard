import pandas as pd
import streamlit as st
from finops.forecast import forecast_two_years
from finops.provider_page import fc_chart
from finops.ui import annual_budget, cfg, get_data, money, pct, require_data, show, sidebar_filters

st.title("Forecast & Budget")
df = sidebar_filters(get_data())
require_data(df)
last = df["date"].max()
yr = last.year
fcfg = cfg()["forecast"]
win = st.sidebar.slider("YoY window (months)", 3, 12, fcfg["yoy_window_months"])
growth = st.sidebar.slider("Next-year adjustment (%)", -30.0, 30.0, float(fcfg["default_growth_pct"]), 0.5,
                           help="Planned savings (-) or new workloads (+) on top of baseline")
provs = sorted(df["provider"].unique())
cur, nxt = forecast_two_years(df, yr, win, growth)
base = forecast_two_years(df, yr, win, 0.0)[1]
mape = cur.attrs["mape"]
st.caption(f"Billed through {last:%b %Y} · each provider modelled separately then summed "
           f"(methods: {cur.attrs['method']}) · "
           + (f"backtest error ≈ {mape:.0%}" if pd.notna(mape) else f"history too short to backtest (band defaults to ±{cfg()['forecast'].get('default_band_pct', 15)}%)")
           + " · bars = low/high band")

st.subheader(f"Current year ({yr})")
b = annual_budget(yr, provs)
a, c, d = st.columns(3)
a.metric("Actual YTD", money(cur[cur.kind == "actual"].value.sum()))
c.metric("Full-year forecast", money(cur.value.sum()), help=f"Range {money(cur.low.sum())} – {money(cur.high.sum())}")
d.metric("vs budget", money(cur.value.sum() - b) if b else "n/a", pct(cur.value.sum() / b - 1) if b else None,
         delta_color="inverse", help=f"Annual budget {money(b)}")
show(fc_chart(cur, f"{yr}: actual + forecast", "#2563EB"), width="stretch")

st.subheader(f"Next year budget ({yr + 1})")
nb = annual_budget(yr + 1, provs)
a, c, d = st.columns(3)
a.metric("Baseline", money(base.value.sum()))
c.metric(f"With {growth:+.1f}% adjustment", money(nxt.value.sum()), money(nxt.value.sum() - base.value.sum()))
d.metric(f"vs {yr} forecast", pct(nxt.value.sum() / cur.value.sum() - 1),
         help=f"{yr + 1} budget set: {money(nb)}" if nb else f"{yr + 1} budget not set yet in config.toml")
show(fc_chart(nxt, f"{yr + 1} budget projection", "#2563EB"), width="stretch")
tbl = nxt.drop(columns="kind").assign(**{k: nxt[k].map(money) for k in ("value", "low", "high")})
tbl.attrs = {}          # forecast metadata may hold NaN, which Streamlit cannot serialize
st.dataframe(tbl, hide_index=True, width="stretch")
st.info("Per-account / per-project / per-service forecasts and budgets: see the AWS and GCP pages → 'View by'.")
