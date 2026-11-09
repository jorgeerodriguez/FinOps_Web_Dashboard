"""Leadership visuals: cumulative build-up by entity + year-over-year on the months that overlap last year."""
from __future__ import annotations
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from .ui import TEMPLATE, annual_budget, current_view, money, show

OTHER = "Other"
PALETTE = ["#2563EB", "#E07B00", "#0F9D8A", "#7C3AED", "#DB2777", "#65A30D", "#0891B2", "#CA8A04", "#475569"]


def _top(d: pd.DataFrame, level: str, n: int) -> list[str]:
    return d.groupby(level)["cost_net"].sum().nlargest(n).index.tolist()


def overlap_months(d: pd.DataFrame, yr: int) -> list[int]:
    """Calendar months billed in BOTH yr and yr-1."""
    cur = set(d[d.date.dt.year == yr].date.dt.month)
    prev = set(d[d.date.dt.year == yr - 1].date.dt.month)
    return sorted(cur & prev)


def entity_block(d: pd.DataFrame, provider: str, level: str, noun: str, topn: int, other_by: str = "Single band",
                 cloud: bool = True) -> dict | None:
    """Everything a cumulative block shows, as data + figures (no Streamlit). Returns None if no data."""
    d = d[d.provider == provider]
    note = None
    if provider == "GCP" and level == "account":
        nm = "Google Support (no project)" if cloud else "(no project): third-party / invoice items"
        d = d.assign(account=d["account"].replace({"(no project)": nm}))
        if (d["account"] == nm).any():
            note = ("**Google Support (no project)** is the Google Support contract: it is billed at the account level, "
                    "not to any project." if cloud else
                    "**(no project)** here holds third-party/reseller charges and invoice-level tax, not tied to any project.")
    if d.empty:
        return None
    last = d.date.max()
    yr = last.year
    cur = d[(d.date.dt.year == yr)]
    top = _top(cur, level, topn)
    cur = cur.assign(ent=cur[level].where(cur[level].isin(top), OTHER))
    others = cur[cur.ent == OTHER]
    n_other = others[level].nunique()
    OTHER_LABEL = f"{OTHER} ({n_other} more)"
    if other_by == "Owner" and n_other:
        own = others.groupby("owner").cost_net.sum().sort_values(ascending=False)
        keep = list(own.index[:4])
        lab = others["owner"].map(lambda o: f"Other · {o}" if o in keep else "Other · remaining owners")
        cur = cur.assign(ent=cur.ent.mask(cur.ent == OTHER, lab))
        other_labels = [f"Other · {o}" for o in keep] + (["Other · remaining owners"] if len(own) > 4 else [])
    else:
        cur = cur.assign(ent=cur.ent.replace({OTHER: OTHER_LABEL}))
        other_labels = [OTHER_LABEL] if n_other else []
    order = top + other_labels
    GRAYS = ["#CBD5E1", "#A8B5C7", "#94A3B8", "#B7C0CC", "#D6DCE5"]
    cmap = {e: (PALETTE[i % len(PALETTE)] if i < len(top) else GRAYS[(i - len(top)) % len(GRAYS)]) for i, e in enumerate(order)}

    ytd_total = cur.cost_net.sum()
    ytd_by = cur.groupby("ent").cost_net.sum()
    ov = overlap_months(d, yr)
    ov_names = ", ".join(pd.Timestamp(yr, m, 1).strftime("%b") for m in ov)

    # ---- takeaway line -------------------------------------------------------------------
    top3 = ytd_by.reindex(top[:3]).sum()
    msg = f"**{provider} {noun}s — YTD {yr}: {money(ytd_total)}.** Top 3 = {top3 / ytd_total:.0%} of spend ({', '.join(t[:26] for t in top[:3])})."

    # ---- YoY on overlap months -----------------------------------------------------------
    yoy = None
    if ov:
        this = d[(d.date.dt.year == yr) & d.date.dt.month.isin(ov)].groupby(level).cost_net.sum()
        prev = d[(d.date.dt.year == yr - 1) & d.date.dt.month.isin(ov)].groupby(level).cost_net.sum()
        yoy = pd.DataFrame({"prev": prev, "this": this}).fillna(0.0).reindex(top).fillna(0.0)
        yoy["chg"] = yoy["this"] - yoy["prev"]
        yoy["pct"] = yoy["chg"] / yoy["prev"].where(yoy["prev"] > 100)
        tot_this, tot_prev = this.sum(), prev.sum()
        riser = yoy["chg"].idxmax() if len(yoy) else None
        msg += (f" Overlap {ov_names}: {money(tot_this)} vs {money(tot_prev)} last year "
                f"({tot_this / tot_prev - 1:+.0%})" if tot_prev else "")
        if riser is not None and yoy.loc[riser, "chg"] > 0:
            msg += f"; biggest riser **{riser[:30]}** ({money(yoy.loc[riser, 'chg'])})."
    # ---- cumulative stacked area ---------------------------------------------------------
    area = None
    if True:
        piv = (cur.pivot_table(index="date", columns="ent", values="cost_net", aggfunc="sum")
               .reindex(pd.date_range(pd.Timestamp(yr, 1, 1), last, freq="MS")).fillna(0.0)).reindex(columns=order, fill_value=0.0)
        cum = piv.cumsum()
        fig = go.Figure()
        for e in order:
            fig.add_scatter(x=cum.index.strftime("%b"), y=cum[e], name=e[:34], stackgroup="one", mode="lines",
                            line=dict(width=0.5, color=cmap[e]), fillcolor=cmap[e],
                            hovertemplate=f"{e[:40]}<br>%{{x}}: $%{{y:,.0f}} cumulative<extra></extra>")
        b = annual_budget(yr, [provider])
        if b:
            fig.add_scatter(x=cum.index.strftime("%b"), y=[b / 12 * (i + 1) for i in range(len(cum))], mode="lines",
                            name=f"{provider} budget (cumulative)", line=dict(color="#dc2626", dash="dash", width=2))
        fig.add_scatter(x=cum.index.strftime("%b"), y=cum.sum(axis=1), mode="lines+markers+text", name="Total",
                        line=dict(color="#0f172a", width=2), text=[""] * (len(cum) - 1) + [money(cum.sum(axis=1).iloc[-1])],
                        textposition="top left", showlegend=False)
        fig.update_layout(template=TEMPLATE, title=f"Cumulative {yr} spend by {noun.lower()}", height=460,
                          yaxis_title="Cumulative net cost ($)", legend=dict(font=dict(size=10)), margin=dict(t=50))
        area = fig

    # ---- dumbbell YoY --------------------------------------------------------------------
    dumbbell = None
    if True:
        if yoy is None or yoy["prev"].sum() == 0:
            pass
        else:
            y = yoy.sort_values("this")
            labels = [e[:24] for e in y.index]
            xmax = max(y["prev"].max(), y["this"].max()) * 1.35          # room for the % labels
            fig2 = go.Figure()
            fig2.add_scatter(x=[v for r in y.itertuples() for v in (r.prev, r.this, None)],
                             y=[v for e in labels for v in (e, e, None)], mode="lines",
                             line=dict(color="#94a3b8", width=3), showlegend=False, hoverinfo="skip")
            fig2.add_scatter(x=y["prev"], y=labels, mode="markers", name=f"{yr - 1}",
                             marker=dict(size=11, color="#f59e0b"), hovertemplate="%{y}<br>%{x:$,.0f}<extra>" + str(yr - 1) + "</extra>")
            fig2.add_scatter(x=y["this"], y=labels, mode="markers+text", name=f"{yr}", marker=dict(size=11, color="#2563EB"),
                             text=[("new" if pd.isna(p) else f"{p:+.0%}") for p in y["pct"]], textposition="middle right",
                             cliponaxis=False, hovertemplate="%{y}<br>%{x:$,.0f}<extra>" + str(yr) + "</extra>")
            fig2.update_layout(template=TEMPLATE, title=f"Same months, {yr - 1} vs {yr} ({ov_names})", height=460,
                               legend=dict(orientation="h", x=1, xanchor="right", y=-0.07, yanchor="top"),
                               margin=dict(t=50, l=10, r=30, b=50), yaxis=dict(automargin=True),
                               xaxis=dict(range=[0, xmax], tickformat="$.2~s", title=None))
            dumbbell = fig2
    other_table, other_caption = None, None
    if n_other:
        om = others.groupby(level).cost_net.sum().sort_values(ascending=False)
        other_caption = (f"{n_other} {noun.lower()}s outside the top {topn} add up to {money(om.sum())} "
                         f"({om.sum() / ytd_total:.0%} of YTD). Largest of them:")
        other_table = pd.DataFrame({f"YTD {yr}": om.head(20).map(money), "share of total": (om.head(20) / ytd_total).map("{:.1%}".format)})
    t = pd.DataFrame({f"YTD {yr}": ytd_by}).assign(share=lambda x: x.iloc[:, 0] / ytd_total)
    if yoy is not None:
        t = t.join(yoy.rename(columns={"prev": f"{yr - 1} ({ov_names})", "this": f"{yr} ({ov_names})", "chg": "change", "pct": "change %"}))
    t = t.sort_values(f"YTD {yr}", ascending=False)
    return dict(msg=msg, note=note, area=area, dumbbell=dumbbell, yr=yr, other_label=OTHER_LABEL, other_table=other_table,
                other_caption=other_caption, table=t)


def cumulative_block(d: pd.DataFrame, provider: str, level: str, noun: str, topn: int, key: str,
                     other_by: str = "Single band") -> None:
    """Render one tab of the Executive Summary's 'Where the money goes' section."""
    blk = entity_block(d, provider, level, noun, topn, other_by, current_view().startswith("Cloud"))
    if blk is None:
        st.info(f"No {provider} data for this selection.")
        return
    st.markdown(blk["msg"])
    if blk["note"]:
        st.info(blk["note"])
    left, right = st.columns([3, 2])
    show(blk["area"], container=left, width="stretch", key=f"{key}_area")
    if blk["dumbbell"] is not None:
        show(blk["dumbbell"], container=right, width="stretch", key=f"{key}_yoy")
    else:
        right.info(f"No overlapping billed months with {blk['yr'] - 1} yet. Appears as history accumulates.")
    if blk["other_table"] is not None:
        with st.expander(f"What's in “{blk['other_label']}”?"):
            st.caption(blk["other_caption"])
            st.dataframe(blk["other_table"], width="stretch")
            st.caption("Raise 'Entities shown per chart' above to promote more of them to their own band.")
    with st.expander("Table & download"):
        st.dataframe(blk["table"].round(3), width="stretch")
        st.download_button("Download CSV", blk["table"].round(2).to_csv().encode(), f"{provider}_{noun}_cumulative.csv", "text/csv", key=f"{key}_dl")
