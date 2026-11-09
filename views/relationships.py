import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from finops.labels import relabel
from finops.ui import COLORS, TEMPLATE, current_view, get_data, heat_scale, money, show, sidebar_filters

st.title("Relationships")
st.caption("Who owns what, and what drives it: owner → account / project → service, sized by spend. "
           f"Spend category: **{current_view()}**.")
df = relabel(sidebar_filters(get_data()))
if df.empty:
    st.info("No data yet — use Data Health → Refresh.")
    st.stop()

last = df.date.max()
period = st.sidebar.radio("Period", ["Year to date", "Last billed month", "Last 3 months", "Last 12 months"])
start = {"Year to date": pd.Timestamp(last.year, 1, 1), "Last billed month": last,
         "Last 3 months": last - pd.DateOffset(months=2), "Last 12 months": last - pd.DateOffset(months=11)}[period]
d = df[(df.date >= start) & (df.cost_net > 0)]
root = st.sidebar.radio("Flow starts from", ["Owner", "Provider", "Family"])
rootcol = root.lower()
nA = st.sidebar.slider("Accounts / projects shown", 5, 30, 14)
nS = st.sidebar.slider("Services shown", 5, 30, 12)
st.sidebar.caption(f"Window: {start:%b %Y} – {last:%b %Y}. Negative lines (credits) are excluded from flows.")

if d.empty:
    st.info("No spend in this window.")
    st.stop()
total = d.cost_net.sum()

# ------------------------------------------------------------------ collapse to top-N
topA = d.groupby("account").cost_net.sum().nlargest(nA).index
topS = d.groupby("service").cost_net.sum().nlargest(nS).index
nOA, nOS = d.account.nunique() - len(topA), d.service.nunique() - len(topS)
LA, LS = f"Other accounts ({nOA} more)", f"Other services ({nOS} more)"
d = d.assign(acct=d.account.where(d.account.isin(topA), LA), svc=d.service.where(d.service.isin(topS), LS))

# ------------------------------------------------------------------ Sankey
st.subheader(f"{root} → Account / Project → Service")
g = d.groupby([rootcol, "acct", "svc"], as_index=False).cost_net.sum()
prov_of = d.groupby("acct").provider.agg(lambda s: s.mode().iat[0])
layers = [list(g.groupby(rootcol).cost_net.sum().sort_values(ascending=False).index),
          list(g.groupby("acct").cost_net.sum().sort_values(ascending=False).index),
          list(g.groupby("svc").cost_net.sum().sort_values(ascending=False).index)]
idx, labels, colors = {}, [], []
for li, names in enumerate(layers):
    for n in names:
        idx[(li, n)] = len(labels)
        val = g[g[[rootcol, "acct", "svc"][li]] == n].cost_net.sum()
        labels.append(f"{n[:38]}  {money(val)}")
        if n.startswith("Other") or n == "(unassigned)":
            colors.append("#CBD5E1")
        elif li == 1:
            colors.append(COLORS.get(prov_of.get(n), "#64748B"))
        else:
            colors.append(["#475569", None, "#0F9D8A"][li])
src, tgt, val, lcol = [], [], [], []
for r in g.groupby([rootcol, "acct"], as_index=False).cost_net.sum().itertuples():
    src.append(idx[(0, getattr(r, rootcol))]); tgt.append(idx[(1, r.acct)]); val.append(r.cost_net)
for r in g.groupby(["acct", "svc"], as_index=False).cost_net.sum().itertuples():
    src.append(idx[(1, r.acct)]); tgt.append(idx[(2, r.svc)]); val.append(r.cost_net)
lcol = ["rgba(148,163,184,0.35)"] * len(val)
fig = go.Figure(go.Sankey(
    node=dict(label=labels, color=colors, pad=14, thickness=16, line=dict(width=0)),
    link=dict(source=src, target=tgt, value=val, color=lcol,
              hovertemplate="%{source.label} → %{target.label}<br>$%{value:,.0f}<extra></extra>")))
fig.update_layout(template=TEMPLATE, height=max(560, 24 * max(len(l) for l in layers) + 120), margin=dict(t=10, l=5, r=5, b=5),
                  font=dict(size=11))
show(fig, width="stretch")
unowned = d[d.owner == "(unassigned)"].cost_net.sum()
st.caption(f"Total in view {money(total)}. Account colors: orange = AWS, blue = GCP. "
           f"{unowned / total:.0%} of this spend has no owner in the owner files "
           "(fill `data/owners_*.csv` to make the left side meaningful).")

# ------------------------------------------------------------------ concentration matrix
st.subheader("Concentration matrix — which services dominate which accounts / projects")
mode = st.radio("Show", ["$ spend", "% of the account's spend"], horizontal=True)
accts = [a for a in d.groupby("account").cost_net.sum().nlargest(nA).index]
svcs = [s for s in d.groupby("service").cost_net.sum().nlargest(nS).index]
m = d[d.account.isin(accts)].pivot_table(index="account", columns="service", values="cost_net", aggfunc="sum", fill_value=0.0)
row_tot = m.sum(axis=1)
m = m.reindex(index=accts, columns=svcs, fill_value=0.0)
z = m.div(row_tot.reindex(m.index), axis=0) if mode.startswith("%") else m
text = z.map(lambda v: "" if v < (0.005 if mode.startswith("%") else 500) else (f"{v:.0%}" if mode.startswith("%") else
                                                                              (f"{v / 1e3:,.0f}K" if v < 1e6 else f"{v / 1e6:.1f}M")))
hm = go.Figure(go.Heatmap(z=z.values, x=[s[:30] for s in z.columns], y=[a[:34] for a in z.index], colorscale=heat_scale("blue"),
                          text=text.values, texttemplate="%{text}", xgap=2, ygap=2, showscale=False,
                          hovertemplate="%{y}<br>%{x}<br>%{z:,.2f}<extra></extra>"))
hm.update_layout(template=TEMPLATE, height=max(380, 26 * len(accts) + 190), margin=dict(t=10, l=10, r=10, b=10),
                 xaxis=dict(side="top", tickangle=-35), yaxis=dict(autorange="reversed", automargin=True))
show(hm, width="stretch")

st.subheader("Biggest account → service pairings")
pr = d.groupby(["provider", "account", "service"], as_index=False).cost_net.sum().nlargest(15, "cost_net")
acct_tot = d.groupby("account").cost_net.sum()
pr["share of account"] = pr.apply(lambda r: r.cost_net / acct_tot[r.account], axis=1).map("{:.0%}".format)
pr["share of total"] = (pr.cost_net / total).map("{:.1%}".format)
st.dataframe(pr.assign(cost_net=pr.cost_net.map(money)).rename(columns={"cost_net": "spend"}), hide_index=True, width="stretch")
