import pandas as pd
import plotly.express as px
import streamlit as st
from finops.data import monthly
from finops.ui import COLORS, TEMPLATE, get_data, money, pct, show

st.title("AWS vs GCP")
df = get_data()
m = monthly(df, ["provider"])
# only compare months present in BOTH providers
common = set(m[m.provider == "AWS"].date) & set(m[m.provider == "GCP"].date)
m = m[m.date.isin(common)]
st.caption(f"Comparable window: {min(common):%b %Y} – {max(common):%b %Y} (months where both providers have data)")

show(px.line(m, x="date", y="cost_net", color="provider", color_discrete_map=COLORS,
                        template=TEMPLATE, markers=True, labels={"cost_net": "Net cost ($)", "date": ""}),
                width="stretch")
p = m.pivot(index="date", columns="provider", values="cost_net")
p["GCP share"] = p["GCP"] / (p["AWS"] + p["GCP"])
a, b = st.columns(2)
show(px.area(p["GCP share"].reset_index(), x="date", y="GCP share", template=TEMPLATE,
                       title="GCP share of total spend"), container=a, width="stretch")
yoy = m.assign(year=m.date.dt.year).groupby(["year", "provider"], as_index=False)["cost_net"].sum()
show(px.bar(yoy, x="year", y="cost_net", color="provider", barmode="group",
                      color_discrete_map=COLORS, template=TEMPLATE, title="Annual totals (comparable months)"),
               container=b, width="stretch")
