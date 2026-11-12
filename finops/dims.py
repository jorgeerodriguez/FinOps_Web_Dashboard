"""Project codes, environments and cost centers: tables, charts and plain-language findings (NO Streamlit; shared by pages and HTML reports).

Everything works on the billing frame after owners.attach_owners, which joins environment / project_code / cost_center / service_name /
finance_notes from the owner files you maintain in Data Health. Windows: the latest 12 billed months vs the 12 before.
"""
from __future__ import annotations
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from .config import load_config
from .owners import MISSING, is_missing
from .ui import TEMPLATE, money, pct

LABEL = {"project_code": "Project code", "environment": "Environment", "cost_center": "Cost center", "service_name": "Service name",
         "family": "Family", "owner": "Owner"}
ENV_COLORS = {"prod": "#2563EB", "non-prod": "#F59E0B", "shared": "#64748B", "unspecified": "#CBD5E1"}
OTHER_COLOR = "#9CA3AF"
NOTE_CHARS = 110


def chg(v: float) -> str:
    """+12% / -30%; a 4x or more increase reads as '57×' (the latest 12 months cost that many times the prior 12)."""
    return "–" if pd.isna(v) else (f"{v + 1:.0f}×" if v >= 3 else f"{v:+.0%}")


# --------------------------------------------------------------------------------------- windows / config
def windows(df: pd.DataFrame):
    last = df["date"].max()
    cur = df[df["date"] > last - pd.DateOffset(months=12)]
    prev = df[(df["date"] <= last - pd.DateOffset(months=12)) & (df["date"] > last - pd.DateOffset(months=24))]
    return cur, prev, last


def shared_codes() -> list[str]:
    """Project codes that are enterprise-wide buckets (support contracts, commitment fees ...), from [dimensions] shared_codes in config.toml.
    They are kept in tables but left out of the share charts by default, because one of them can be half the bill."""
    return [str(x) for x in load_config().get("dimensions", {}).get("shared_codes", [])]


def is_shared(codes: pd.Series, shared: list[str] | None = None) -> pd.Series:
    sh = {x.lower() for x in (shared_codes() if shared is None else shared)}
    return codes.str.lower().isin(sh)


# --------------------------------------------------------------------------------------- coverage
def coverage(df: pd.DataFrame, col: str) -> dict:
    """Share of the latest 12 months' spend that has a real value in `col`, and the biggest rows that do not."""
    cur, _, _ = windows(df)
    miss = is_missing(cur[col], col)
    total = float(cur["cost_net"].sum())
    gone = float(cur.loc[miss, "cost_net"].sum())
    worst = (cur[miss].groupby(["provider", "account"])["cost_net"].sum().sort_values(ascending=False).head(10).reset_index()
             .rename(columns={"provider": "Provider", "account": "Account / project", "cost_net": "Spend without it"}))
    return dict(col=col, total=total, missing=gone, share=(1 - gone / total) if total else 0.0, worst=worst)


def readiness(df: pd.DataFrame) -> pd.DataFrame:
    """One row per provider x field: how much of the spend carries a project code / environment / cost center."""
    rows = []
    for prov in sorted(df["provider"].unique()):
        d = df[df["provider"] == prov]
        for col in ("project_code", "environment", "cost_center"):
            c = coverage(d, col)
            rows.append({"Provider": prov, "Field": LABEL[col], "Spend covered": c["share"], "Spend without it": c["missing"]})
    return pd.DataFrame(rows)


def readiness_display(df: pd.DataFrame) -> pd.DataFrame:
    r = readiness(df)
    return r.assign(**{"Spend covered": r["Spend covered"].map(lambda v: f"{v:.0%}"), "Spend without it": r["Spend without it"].map(money)})


# --------------------------------------------------------------------------------------- tables
def _notes(d: pd.DataFrame, col: str) -> pd.Series:
    n = d[d["finance_notes"] != ""].groupby(col)["finance_notes"].agg(lambda s: " · ".join(list(dict.fromkeys(s))[:2]))
    return n.map(lambda t: t if len(t) <= NOTE_CHARS else t[: NOTE_CHARS - 1] + "…")


def by_dim(df: pd.DataFrame, col: str, shared: list[str] | None = None) -> pd.DataFrame:
    """Latest 12 months vs the 12 before, per value of `col`, largest first. Numeric columns; use display() for text."""
    cur, prev, _ = windows(df)
    c, p = cur.groupby(col)["cost_net"].sum(), prev.groupby(col)["cost_net"].sum()
    t = pd.DataFrame({"cur": c, "prev": p}).fillna(0.0)
    t["change"] = t["cur"] - t["prev"]
    t["change_pct"] = (t["change"] / t["prev"].where(t["prev"].abs() >= 1000)).astype(float)
    t["share"] = t["cur"] / t["cur"].sum() if t["cur"].sum() else 0.0
    prov = cur.groupby(col)["provider"].agg(lambda s: " + ".join(sorted(set(s))))
    t["providers"] = prov
    t["accounts"] = cur.groupby(col)["account"].nunique()
    t["notes"] = _notes(cur, col)
    t = t.fillna({"providers": "", "accounts": 0, "notes": ""})
    if col == "project_code":
        t["shared"] = is_shared(pd.Series(t.index, index=t.index), shared)
    t = t.sort_values("cur", ascending=False)
    t.index.name = col
    return t


def display(t: pd.DataFrame, col: str) -> pd.DataFrame:
    out = pd.DataFrame({LABEL[col]: [("★ " if t.at[i, "shared"] else "") + str(i) if "shared" in t else str(i) for i in t.index],
                        "Clouds": t["providers"].values, "Latest 12 mo": t["cur"].map(money).values, "Share": t["share"].map(lambda v: f"{v:.1%}").values,
                        "Prior 12 mo": t["prev"].map(money).values, "Change": t["change"].map(lambda v: ("+" if v > 0 else "") + money(v)).values,
                        "Change %": [("new" if c >= 1000 else "–") if pd.isna(v) else chg(v) for v, c in zip(t["change_pct"], t["cur"])],
                        "Accounts": t["accounts"].astype(int).values, "Finance notes": t["notes"].values})
    return out.reset_index(drop=True)


def monthly(df: pd.DataFrame, col: str, topn: int = 10, months: int = 24) -> pd.DataFrame:
    """Month x value of `col` for the biggest `topn` (rest = 'Other'), long form."""
    last = df["date"].max()
    d = df[df["date"] > last - pd.DateOffset(months=months)]
    top = d.groupby(col)["cost_net"].sum().nlargest(topn).index
    d = d.assign(**{col: d[col].where(d[col].isin(top), "Other")})
    return d.groupby(["date", col], as_index=False)["cost_net"].sum()


def env_by_code(df: pd.DataFrame, col: str = "project_code") -> pd.DataFrame:
    """Per project code: spend by environment, non-prod share, and whether non-prod is growing (last 6 months vs the 6 before)."""
    cur, _, last = windows(df)
    piv = cur.pivot_table(index=col, columns="environment", values="cost_net", aggfunc="sum", fill_value=0.0)
    for e in ENV_COLORS:
        if e not in piv:
            piv[e] = 0.0
    piv["total"] = piv.sum(axis=1)
    known = piv["prod"] + piv["non-prod"]
    piv["nonprod_share"] = (piv["non-prod"] / known.where(known > 0)).astype(float)
    np_ = df[df["environment"] == "non-prod"]
    a = np_[(np_["date"] > last - pd.DateOffset(months=6))].groupby(col)["cost_net"].sum()
    b = np_[(np_["date"] <= last - pd.DateOffset(months=6)) & (np_["date"] > last - pd.DateOffset(months=12))].groupby(col)["cost_net"].sum()
    piv["nonprod_6m_change"] = ((a - b) / b.where(b >= 1000)).reindex(piv.index).astype(float)
    return piv.sort_values("total", ascending=False)


def env_display(e: pd.DataFrame, col: str = "project_code") -> pd.DataFrame:
    return pd.DataFrame({LABEL[col]: e.index, "Prod": e["prod"].map(money).values, "Non-prod": e["non-prod"].map(money).values,
                         "Shared": e["shared"].map(money).values, "Unspecified": e["unspecified"].map(money).values,
                         "Non-prod % of prod + non-prod": e["nonprod_share"].map(lambda v: "–" if pd.isna(v) else f"{v:.0%}").values,
                         "Non-prod, last 6 mo vs 6 before": e["nonprod_6m_change"].map(chg).values}).reset_index(drop=True)


def cross_cloud(df: pd.DataFrame, min_each: float = 5_000.0) -> pd.DataFrame:
    """Project codes that run on BOTH clouds (at least `min_each` a year on each): the combined cost of one project."""
    cur, prev, _ = windows(df)
    p = cur.pivot_table(index="project_code", columns="provider", values="cost_net", aggfunc="sum", fill_value=0.0)
    for c in ("AWS", "GCP"):
        if c not in p:
            p[c] = 0.0
    p = p[(p["AWS"] >= min_each) & (p["GCP"] >= min_each)]
    p = p[~is_missing(pd.Series(p.index, index=p.index), "project_code")]
    p["total"] = p["AWS"] + p["GCP"]
    pt = pd.Series(p.index, index=p.index).map(prev.groupby("project_code")["cost_net"].sum()).astype(float)
    p["change_pct"] = ((p["total"] - pt.fillna(0.0)) / pt.where(pt >= 1000)).astype(float)
    return p.sort_values("total", ascending=False)


def cross_display(p: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({"Project code": p.index, "AWS": p["AWS"].map(money).values, "GCP": p["GCP"].map(money).values, "Together": p["total"].map(money).values,
                         "GCP share": (p["GCP"] / p["total"]).map(lambda v: f"{v:.0%}").values,
                         "Change vs prior 12 mo": p["change_pct"].map(chg).values}).reset_index(drop=True)


# --------------------------------------------------------------------------------------- charts
def fig_stack(df: pd.DataFrame, col: str, topn: int = 10, months: int = 24, title: str | None = None) -> go.Figure:
    m = monthly(df, col, topn, months)
    fig = px.bar(m, x="date", y="cost_net", color=col, template=TEMPLATE, title=title or f"Monthly cost by {LABEL[col].lower()}",
                 labels={"cost_net": "Net cost ($)", "date": "", col: LABEL[col]}, color_discrete_map={"Other": OTHER_COLOR},
                 color_discrete_sequence=px.colors.qualitative.Safe)
    fig.update_layout(legend=dict(orientation="h", y=-0.2, font=dict(size=10)), bargap=0.15)
    return fig


def fig_change(t: pd.DataFrame, col: str, n: int = 12, title: str | None = None) -> go.Figure:
    """Horizontal bars: latest 12 months per value, labelled with the change vs the prior 12."""
    d = t.head(n).iloc[::-1]
    lab = [f"{money(c)}  ({'new' if p < 1000 else chg(ch)})" for c, p, ch in zip(d["cur"], d["prev"], d["change_pct"].fillna(0))]
    colors = [("#B91C1C" if ch > 0.15 else "#15803D" if ch < -0.15 else "#64748B") if p >= 1000 else "#B91C1C"
              for ch, p in zip(d["change_pct"].fillna(0), d["prev"])]
    fig = go.Figure(go.Bar(x=d["cur"], y=d.index.astype(str), orientation="h", text=lab, textposition="outside", marker_color=colors, cliponaxis=False))
    fig.update_layout(template=TEMPLATE, title=title or f"Latest 12 months by {LABEL[col].lower()} (change vs prior 12 in brackets)", xaxis_title="Net cost ($)", yaxis_title="",
                      margin=dict(r=140), height=max(280, 26 * len(d) + 110))
    fig.update_xaxes(range=[0, d["cur"].max() * 1.35 if len(d) else 1])
    return fig


def fig_env(df: pd.DataFrame, months: int = 24) -> go.Figure:
    last = df["date"].max()
    d = df[df["date"] > last - pd.DateOffset(months=months)].groupby(["date", "environment"], as_index=False)["cost_net"].sum()
    order = [e for e in ENV_COLORS if e in set(d["environment"])] + sorted(set(d["environment"]) - set(ENV_COLORS))
    fig = px.bar(d, x="date", y="cost_net", color="environment", template=TEMPLATE, category_orders={"environment": order},
                 color_discrete_map=ENV_COLORS, title="Monthly cost by environment", labels={"cost_net": "Net cost ($)", "date": "", "environment": "Environment"})
    fig.update_layout(legend=dict(orientation="h", y=-0.2), bargap=0.15)
    return fig


def fig_nonprod_share(df: pd.DataFrame, months: int = 24) -> go.Figure:
    last = df["date"].max()
    d = df[(df["date"] > last - pd.DateOffset(months=months)) & df["environment"].isin(["prod", "non-prod"])]
    p = d.pivot_table(index="date", columns="environment", values="cost_net", aggfunc="sum", fill_value=0.0)
    if "non-prod" not in p or "prod" not in p:
        return go.Figure().update_layout(template=TEMPLATE, title="Non-prod share (needs both prod and non-prod rows)")
    share = p["non-prod"] / (p["prod"] + p["non-prod"])
    fig = go.Figure(go.Scatter(x=share.index, y=share.values, mode="lines+markers", line=dict(color=ENV_COLORS["non-prod"], width=3)))
    fig.update_layout(template=TEMPLATE, title="Non-prod as a share of prod + non-prod spend", yaxis=dict(tickformat=".0%", rangemode="tozero"), xaxis_title="")
    return fig


TREE_PALETTE = ["#2563EB", "#DB2777", "#059669", "#D97706", "#7C3AED", "#0891B2", "#DC2626", "#65A30D", "#9333EA", "#0F766E", "#C2410C", "#4F46E5"]
TREE_OTHER = "#6B7280"        # "Other project codes" and "(no project code)": grey = not a real project


def _mix(hex_color: str, toward_white: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    f = lambda c: round(c + (255 - c) * toward_white)
    return f"#{f(r):02x}{f(g):02x}{f(b):02x}"


def fig_treemap(df: pd.DataFrame, shared: list[str] | None = None, leave_out_shared: bool = True, max_codes: int = 12) -> go.Figure:
    """Project code (one color each) -> account / project, sized by the latest 12 months. Header strip = the code, tinted tiles = its accounts,
    each labelled with its cloud. Small accounts are folded into one tile per code. Colors are fixed (not theme-dependent) so it reads on light and dark."""
    cur, _, _ = windows(df)
    d = cur[cur["cost_net"] > 0]
    if leave_out_shared:
        d = d[~is_shared(d["project_code"], shared)]
    d = d.groupby(["project_code", "provider", "account"], as_index=False)["cost_net"].sum()
    if d.empty:
        return go.Figure().update_layout(template=TEMPLATE, title="Nothing to show for the latest 12 months")
    total = float(d["cost_net"].sum())
    order = d.groupby("project_code")["cost_net"].sum().sort_values(ascending=False)
    keep = list(order.index[:max_codes])
    d["code"] = d["project_code"].where(d["project_code"].isin(keep), "Other project codes")
    ids, labels, parents, values, colors, texts, tcolors, hover = [], [], [], [], [], [], [], []
    codes = list(d.groupby("code")["cost_net"].sum().sort_values(ascending=False).index)
    for i, code in enumerate(codes):
        g = d[d["code"] == code].sort_values("cost_net", ascending=False)
        base = TREE_OTHER if code in ("Other project codes", MISSING["project_code"]) else TREE_PALETTE[keep.index(code) % len(TREE_PALETTE)]
        ctotal = float(g["cost_net"].sum())
        floor = max(0.07 * ctotal, 0.005 * total)                           # folded into one tile when smaller than this
        big, small = g[g["cost_net"] >= floor], g[g["cost_net"] < floor]
        clouds = " + ".join(sorted(set(g["provider"])))
        ids.append(code); labels.append(code); parents.append(""); values.append(ctotal); colors.append(base)
        texts.append(f"<b>{code}</b> · {money(ctotal)} · {ctotal / total:.0%}"); tcolors.append("#ffffff")
        hover.append(f"<b>{code}</b><br>{money(ctotal)} · {ctotal / total:.1%} of the total<br>{clouds}")
        for j, r in enumerate(big.itertuples()):
            ids.append(f"{code}||{r.provider}||{r.account}"); labels.append(r.account); parents.append(code); values.append(float(r.cost_net))
            colors.append(_mix(base, 0.55 if r.provider == "AWS" else 0.72)); tcolors.append("#111111")
            texts.append(f"<b>{r.account}</b><br>{r.provider} · {money(r.cost_net)}")
            hover.append(f"<b>{r.account}</b><br>{r.provider} · {money(r.cost_net)}<br>{r.cost_net / ctotal:.0%} of {code}")
        if len(small):
            ids.append(f"{code}||small"); labels.append("smaller"); parents.append(code); values.append(float(small["cost_net"].sum()))
            colors.append(_mix(base, 0.85)); tcolors.append("#111111")
            texts.append(f"{len(small)} smaller<br>{money(small['cost_net'].sum())}")
            hover.append(f"{len(small)} smaller accounts / projects<br>{money(small['cost_net'].sum())}")
    fig = go.Figure(go.Treemap(ids=ids, labels=labels, parents=parents, values=values, branchvalues="total", text=texts, textinfo="text",
                               hovertext=hover, hoverinfo="text", marker=dict(colors=colors, line=dict(width=1.5, color="#94a3b8"), pad=dict(t=26, l=4, r=4, b=4)),
                               textfont=dict(color=tcolors, size=13), pathbar=dict(visible=False), tiling=dict(packing="squarify"), sort=False))
    fig.update_layout(template=TEMPLATE, title="Where the money goes: project code → account / project (latest 12 months; lighter tile = AWS, lighter still = GCP)",
                      height=560, margin=dict(t=50, l=4, r=4, b=4))
    return fig


def fig_cross(p: pd.DataFrame, n: int = 10) -> go.Figure:
    d = p.head(n).iloc[::-1]
    fig = go.Figure()
    fig.add_bar(y=d.index, x=d["AWS"], name="AWS", orientation="h", marker_color="#E07B00")
    fig.add_bar(y=d.index, x=d["GCP"], name="GCP", orientation="h", marker_color="#2563EB")
    fig.update_layout(template=TEMPLATE, barmode="stack", title="Projects that run on both clouds (latest 12 months)", xaxis_title="Net cost ($)",
                      legend=dict(orientation="h", y=-0.35), height=max(260, 30 * len(d) + 120))
    return fig


# --------------------------------------------------------------------------------------- findings in words
def kpi_items(df: pd.DataFrame, shared: list[str] | None = None) -> list[dict]:
    cur, prev, _ = windows(df)
    pc = coverage(df, "project_code")
    t = by_dim(df, "project_code", shared)
    real = t[~t.index.isin([MISSING["project_code"]])]
    non_shared = real[~real["shared"]] if "shared" in real else real
    top = non_shared.iloc[0] if len(non_shared) else None
    env = cur.groupby("environment")["cost_net"].sum()
    known = env.get("prod", 0.0) + env.get("non-prod", 0.0)
    items = [
        dict(label="Project codes", value=str(len(real)), delta=f"{pc['share']:.0%} of spend coded", neutral=True,
             help="Distinct project codes with spend in the latest 12 months, and the share of that spend that has a code"),
        dict(label="Biggest project", value=str(top.name) if top is not None else "–", delta=(f"{money(top['cur'])} · {top['share']:.0%}" if top is not None else ""), neutral=True,
             help="Largest project code, leaving out shared / enterprise buckets"),
        dict(label="Non-prod share", value=f"{env.get('non-prod', 0.0) / known:.0%}" if known else "–", delta="of prod + non-prod", neutral=True,
             help="Spend on non-production environments as a share of prod + non-prod. Spend whose environment is unspecified or shared is not counted."),
        dict(label="Environment unspecified", value=f"{env.get('unspecified', 0.0) / env.sum():.0%}" if env.sum() else "–", delta=money(env.get("unspecified", 0.0)), neutral=True,
              help="Spend whose account / project has no environment in the owner file"),
        dict(label="Cost center coverage", value=f"{coverage(df, 'cost_center')['share']:.0%}", delta="of spend", neutral=True,
             help="Share of spend with a cost center in the owner file"),
    ]
    return items


def narrative(df: pd.DataFrame, shared: list[str] | None = None) -> list[str]:
    """Plain-language findings about project codes and environments (latest 12 months vs the 12 before)."""
    cur, prev, _ = windows(df)
    out = []
    t = by_dim(df, "project_code", shared)
    miss = MISSING["project_code"]
    real = t[t.index != miss]
    sh = real[real["shared"]] if "shared" in real else real.iloc[0:0]
    main = real[~real["shared"]] if "shared" in real else real
    tot = float(cur["cost_net"].sum())
    if len(sh):
        out.append(f"**Shared buckets:** {', '.join(f'{i} ({money(r.cur)})' for i, r in sh.iterrows())} are {sh['cur'].sum() / tot:.0%} of spend and are left out of the project charts. "
                   "They are enterprise-wide (support contracts, commitment fees), not a project's own cost.")
    if len(main):
        top3 = main.head(3)
        out.append(f"**Biggest projects:** " + ", ".join(f"{i} {money(r.cur)}" for i, r in top3.iterrows())
                   + f" — together {top3['cur'].sum() / main['cur'].sum():.0%} of project-coded spend.")
        up = main[(main["change"] >= 25_000) & (main["change_pct"].fillna(1) >= 0.15)].sort_values("change", ascending=False).head(3)
        dn = main[(main["change"] <= -25_000) & (main["change_pct"].fillna(-1) <= -0.15)].sort_values("change").head(3)
        if len(up):
            out.append("**Growing:** " + ", ".join(f"{i} {('+' + money(r.change))} ({'new' if r.prev < 1000 else chg(r.change_pct)})" for i, r in up.iterrows()) + " vs the prior 12 months.")
        if len(dn):
            out.append("**Shrinking:** " + ", ".join(f"{i} {money(r.change)} ({r.change_pct:+.0%})" for i, r in dn.iterrows()) + " vs the prior 12 months.")
    cc = cross_cloud(df)
    if len(cc):
        out.append(f"**On both clouds:** {len(cc)} project(s) bill on AWS and GCP — " + ", ".join(f"{i} {money(r.total)}" for i, r in cc.head(3).iterrows())
                   + ". Looking at one cloud alone understates them.")
    e = cur.groupby("environment")["cost_net"].sum()
    known = e.get("prod", 0.0) + e.get("non-prod", 0.0)
    if known:
        out.append(f"**Environments:** non-prod is {e.get('non-prod', 0.0) / known:.0%} of prod + non-prod spend ({money(e.get('non-prod', 0.0))}).")
    unspec = e.get("unspecified", 0.0)
    if tot and unspec / tot >= 0.05:
        out.append(f"**Gap:** {unspec / tot:.0%} of spend ({money(unspec)}) has no environment, so the prod / non-prod split understates both. Fill them in Data Health → Owners.")
    pc = coverage(df, "project_code")
    if pc["missing"] / tot >= 0.02 if tot else False:
        out.append(f"**Gap:** {money(pc['missing'])} ({1 - pc['share']:.0%}) of spend has no project code.")
    return out
