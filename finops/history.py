"""Two-year history: numbers, figures and tables with NO Streamlit in them.
Used by views/history.py (the page), finops/history_report.py and the Executive report, so they can never disagree.

Everything is measured on rolling 12-month windows (latest 12 billed months vs the 12 before) and on matching calendar
months, so a partial year never gets compared with a full one."""
from __future__ import annotations
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .ui import COLORS, TEMPLATE, annual_budget, money, pct

YEAR_COLORS = {0: "#94a3b8", 1: "#f59e0b", 2: "#2563EB", 3: "#0F9D8A"}     # oldest year -> newest
SPIKE_RATIO, SPIKE_MIN = 1.35, 25_000        # a month is a spike if it is 35% above its neighbours AND by at least $25K


@dataclass
class History:
    df: pd.DataFrame
    first: pd.Timestamp
    last: pd.Timestamp
    provs: list
    m: pd.DataFrame                  # month x provider (+ "Total")
    cur: pd.DatetimeIndex            # latest 12 billed months
    prev: pd.DatetimeIndex           # the 12 before
    has_prev: bool
    spikes: pd.DataFrame = None
    notes: list = field(default_factory=list)

    @property
    def yr(self) -> int:
        return self.last.year


# --------------------------------------------------------------------------------------- build
def compute(df: pd.DataFrame) -> History:
    df = df[~df["is_partial"].astype(bool)] if "is_partial" in df else df
    first, last = df["date"].min(), df["date"].max()
    provs = [p for p in ("AWS", "GCP") if p in set(df["provider"])]
    full = pd.date_range(first, last, freq="MS")
    m = df.pivot_table(index="date", columns="provider", values="cost_net", aggfunc="sum").reindex(full).fillna(0.0)
    m = m.reindex(columns=provs, fill_value=0.0)
    m["Total"] = m[provs].sum(axis=1)
    cur = pd.date_range(last - pd.DateOffset(months=11), last, freq="MS")
    prev = pd.date_range(last - pd.DateOffset(months=23), last - pd.DateOffset(months=12), freq="MS")
    h = History(df=df, first=first, last=last, provs=provs, m=m, cur=cur, prev=prev, has_prev=bool(first <= prev[0]))
    h.spikes = find_spikes(h)
    return h


def r12(h: History, col: str = "Total") -> tuple[float, float]:
    return h.m.loc[h.cur, col].sum(), (h.m.loc[h.prev, col].sum() if h.has_prev else np.nan)


def ytd_pair(h: History, col: str = "Total") -> tuple[float, float]:
    """This year's YTD vs the same calendar months last year."""
    mo = h.last.month
    a = h.m[(h.m.index.year == h.yr) & (h.m.index.month <= mo)][col].sum()
    ly = h.m[(h.m.index.year == h.yr - 1) & (h.m.index.month <= mo)]
    return a, (ly[col].sum() if len(ly) == mo else np.nan)


def find_spikes(h: History, window: int = 3) -> pd.DataFrame:
    """Months that stand well above (or below) the median of the 3 months either side. Names the services behind each one."""
    rows = []
    sv = h.df.pivot_table(index="date", columns=["provider", "service"], values="cost_net", aggfunc="sum").reindex(h.m.index).fillna(0.0)
    idx = list(h.m.index)
    for p in h.provs:
        s = h.m[p]
        for i, d in enumerate(idx):
            nb = [idx[j] for j in range(max(0, i - window), min(len(idx), i + window + 1)) if j != i]
            if len(nb) < 4:
                continue
            normal = float(s.reindex(nb).median())
            diff = float(s[d] - normal)
            if normal > 0 and abs(diff) >= SPIKE_MIN and (s[d] / normal >= SPIKE_RATIO or s[d] / normal <= 1 / SPIKE_RATIO):
                cols = sv[p] if p in sv.columns.get_level_values(0) else pd.DataFrame()
                chg = (cols.loc[d] - cols.reindex(nb).median()).sort_values(ascending=diff > 0)
                top = chg.iloc[::-1] if diff > 0 else chg
                top = top.head(2)
                why = "; ".join(f"{k[:34]} ({money(v)})" for k, v in top.items() if abs(v) >= 0.15 * abs(diff))
                rows.append(dict(month=d, provider=p, billed=float(s[d]), normal=normal, difference=diff,
                                 direction="Up" if diff > 0 else "Down", driver=why or "spread across many services"))
    out = pd.DataFrame(rows, columns=["month", "provider", "billed", "normal", "difference", "direction", "driver"])
    if len(out):                                   # a dip right after a spike is the rebound, not a finding of its own
        ups = {(r.provider, r.month) for r in out.itertuples() if r.direction == "Up"}
        out = out[~out.apply(lambda r: r.direction == "Down" and (r.provider, r.month - pd.DateOffset(months=1)) in ups, axis=1)]
    return out.reset_index(drop=True)


# --------------------------------------------------------------------------------------- headline numbers
def kpi_items(h: History) -> list[dict]:
    cur, prev = r12(h)
    ytd, ytd_ly = ytd_pair(h)
    tot = h.m["Total"]
    three, three_ly = tot.tail(3).mean(), tot.reindex(tot.tail(3).index - pd.DateOffset(years=1)).mean()
    fyp = h.m[h.m.index.year == h.yr - 1]
    full_prev = len(fyp) == 12
    peak = tot.idxmax()
    return [
        dict(label="Last 12 months", value=money(cur), delta=pct(cur / prev - 1) if prev == prev and prev else "",
             help=f"{h.cur[0]:%b %Y} – {h.last:%b %Y}, vs the 12 months before ({h.prev[0]:%b %Y} – {h.prev[-1]:%b %Y})" if h.has_prev else ""),
        dict(label=f"FY{h.yr - 1}" + ("" if full_prev else " (partial)"), value=money(fyp["Total"].sum()), delta="", neutral=True,
             help="Calendar year, Jan–Dec" if full_prev else f"Data starts {h.first:%b %Y}"),
        dict(label=f"YTD {h.yr}", value=money(ytd), delta=pct(ytd / ytd_ly - 1) if ytd_ly == ytd_ly and ytd_ly else "",
             help=f"vs {money(ytd_ly)} for the same months of {h.yr - 1}" if ytd_ly == ytd_ly else ""),
        dict(label="Exit run-rate (3-mo avg)", value=money(three), delta=pct(three / three_ly - 1) if three_ly == three_ly and three_ly else "",
             help="Average of the last 3 billed months, vs the same 3 months a year earlier"),
        dict(label="Peak month", value=money(tot.max()), delta=f"{peak:%b %Y}", neutral=True,
             help=f"Highest month in the {len(tot)} months we hold"),
    ]


def narrative(h: History, allc: pd.DataFrame | None = None) -> list[str]:
    """Plain-language findings for leadership. Every number comes from the same functions as the charts."""
    out = []
    cur, prev = r12(h)
    if h.has_prev and prev:
        out.append(f"**Last 12 months: {money(cur)}**, {cur / prev - 1:+.1%} vs the 12 months before ({money(prev)}).")
        parts = []
        for p in h.provs:
            c, pv = r12(h, p)
            if pv:
                parts.append(f"{p} {c / pv - 1:+.0%} ({money(pv)} → {money(c)})")
        if parts:
            out.append("By provider: " + ", ".join(parts) + ".")
    tot = h.m["Total"]
    sp = h.spikes[(h.spikes.direction == "Up")].sort_values("difference", ascending=False) if len(h.spikes) else h.spikes
    inprev = sp[sp.month.isin(h.prev)] if len(sp) else sp
    if len(inprev) and h.has_prev:
        x = inprev.iloc[0]
        ex = prev - float(inprev.difference.sum())
        out.append(f"**One-off in {x.month:%b %Y}:** {x.provider} billed {money(x.billed)} vs ~{money(x.normal)} normally ({money(x.difference)} extra; "
                   f"driver: {x.driver}). It sits in the prior 12 months, so growth looks lower than it is — "
                   f"without it the prior year is ~{money(ex)} and growth is **{cur / ex - 1:+.1%}**.")
    for p in h.provs:
        s = h.m[p]
        lo_i, hi_i = s.tail(h.m.shape[0]).idxmin(), s.idxmax()
        out.append(f"**{p}:** low {money(s.min())} ({lo_i:%b %Y}), high {money(s.max())} ({hi_i:%b %Y}); latest month {money(s.iloc[-1])}.")
    ytd, ytd_ly = ytd_pair(h)
    if ytd_ly == ytd_ly and ytd_ly:
        out.append(f"**YTD {h.yr} (Jan–{h.last:%b}): {money(ytd)}**, {ytd / ytd_ly - 1:+.1%} vs the same months of {h.yr - 1} ({money(ytd_ly)}).")
    b = annual_budget(h.yr, h.provs)
    if b:
        out.append(f"{h.yr} budget is {money(b)} ({money(b / 12)}/month); YTD spend is {ytd / (b / 12 * h.last.month):.0%} of the budget to date.")
    return out


# --------------------------------------------------------------------------------------- figures
def fig_trend(h: History) -> go.Figure:
    """Every billed month, stacked by provider, with the rolling 12-month average and the budget where one exists."""
    fig = go.Figure()
    x = h.m.index
    for p in h.provs:
        fig.add_bar(x=x, y=h.m[p], name=p, marker_color=COLORS[p], hovertemplate="%{y:$,.0f}")
    roll = h.m["Total"].rolling(12).mean()
    fig.add_scatter(x=x, y=roll, name="12-month average", mode="lines", line=dict(color="#0f172a", width=2.5),
                    hovertemplate="12-mo avg %{y:$,.0f}")
    b = [annual_budget(d.year, h.provs) / 12 for d in x]
    if any(b):
        fig.add_scatter(x=x, y=[v or None for v in b], name="Budget / month", mode="lines", line=dict(color="#dc2626", dash="dash", width=2),
                        line_shape="hv", hovertemplate="Budget %{y:$,.0f}")
    up = h.spikes[h.spikes.direction == "Up"].sort_values("difference", ascending=False).head(3) if len(h.spikes) else h.spikes
    for r in up.itertuples():
        fig.add_annotation(x=r.month, y=h.m.loc[r.month, "Total"], text=f"{r.provider} one-off<br>+{money(r.difference)}", showarrow=True,
                           arrowhead=2, ax=0, ay=-34, font=dict(size=10, color="#334155"))
    fig.update_layout(template=TEMPLATE, barmode="stack", height=420, margin=dict(t=30, b=10), hovermode="x unified",
                      legend=dict(orientation="h", y=-0.12), yaxis=dict(tickformat="$.2~s"), xaxis=dict(tickformat="%b %y", dtick="M2"))
    return fig


def fig_yoy(h: History) -> go.Figure:
    """Jan–Dec lines, one per year, for each provider and the total: the same month, year over year."""
    cols = h.provs + ["Total"]
    fig = make_subplots(rows=1, cols=len(cols), subplot_titles=cols, shared_yaxes=False, horizontal_spacing=0.07)
    years = sorted(set(h.m.index.year))
    for ci, c in enumerate(cols, 1):
        for i, y in enumerate(years):
            s = h.m[h.m.index.year == y][c]
            age = len(years) - 1 - i                                  # 0 = newest
            fig.add_scatter(x=s.index.month, y=s.values, mode="lines+markers", name=str(y), legendgroup=str(y), showlegend=(ci == 1),
                            line=dict(color=YEAR_COLORS[len(years) - 1 - age] if len(years) <= 4 else "#64748b", width=3 if age == 0 else 2,
                                      dash="solid" if age <= 1 else "dot"), marker=dict(size=6), hovertemplate=f"{y} %{{x}}: %{{y:$,.0f}}<extra></extra>",
                            row=1, col=ci)
    fig.update_xaxes(tickmode="array", tickvals=list(range(1, 13)), ticktext=list("JFMAMJJASOND"))
    fig.update_yaxes(tickformat="$.2~s", rangemode="tozero")
    fig.update_layout(template=TEMPLATE, height=330, margin=dict(t=40, b=10), legend=dict(orientation="h", y=-0.16))
    return fig


def fig_rolling(h: History) -> go.Figure:
    """Cost of the most recent 12 months at every month-end: where the annual run-rate is heading."""
    fig = go.Figure()
    r = h.m[h.provs].rolling(12).sum().dropna()
    for p in h.provs:
        fig.add_scatter(x=r.index, y=r[p], name=p, stackgroup="one", mode="lines", line=dict(width=0.5, color=COLORS[p]), fillcolor=COLORS[p],
                        hovertemplate="%{y:$,.0f}")
    tot = r.sum(axis=1)
    fig.add_scatter(x=tot.index, y=tot, name="Total", mode="lines+markers+text", line=dict(color="#0f172a", width=2),
                    text=[""] * (len(tot) - 1) + [money(tot.iloc[-1])], textposition="top left", showlegend=False)
    b = annual_budget(h.yr, h.provs)
    if b:
        fig.add_hline(y=b, line=dict(color="#dc2626", dash="dash"), annotation_text=f"{h.yr} budget {money(b)}", annotation_position="top left", annotation_font_size=10)
    fig.update_layout(template=TEMPLATE, height=330, margin=dict(t=30, b=10), hovermode="x unified", legend=dict(orientation="h", y=-0.15),
                      yaxis=dict(tickformat="$.2~s"), xaxis=dict(tickformat="%b %y"))
    return fig


# --------------------------------------------------------------------------------------- what changed
def movers(h: History, level: str = "service", n: int = 12) -> pd.DataFrame:
    """Biggest changes between the prior 12 months and the latest 12, by service or account/project, per provider."""
    if not h.has_prev:
        return pd.DataFrame()
    d = h.df
    a = d[d.date.isin(h.cur)].groupby(["provider", level]).cost_net.sum()
    b = d[d.date.isin(h.prev)].groupby(["provider", level]).cost_net.sum()
    t = pd.DataFrame({"prior": b, "latest": a}).fillna(0.0)
    t["change"] = t.latest - t.prior
    t["pct"] = (t.change / t.prior.where(t.prior > 1000)).round(4)
    t = t.reset_index().rename(columns={level: "name"})
    t["status"] = np.where(t.prior < 1000, "New", np.where(t.latest < 0.1 * t.prior, "Ended / near zero", ""))
    return t.reindex(t.change.abs().sort_values(ascending=False).index).head(n)


def fig_bridge(h: History, n: int = 6) -> go.Figure | None:
    """Waterfall from the prior 12 months to the latest 12, by service: what explains the change."""
    if not h.has_prev:
        return None
    t = movers(h, "service", 400)
    t["label"] = t.provider + " " + t.name.str.replace("Amazon ", "").str.replace("Google ", "").str.slice(0, 18)
    big = t.reindex(t.change.abs().sort_values(ascending=False).index).head(n)
    other = t.change.sum() - big.change.sum()
    prior, latest = r12(h)[1], r12(h)[0]
    labels = ["Prior 12 mo"] + list(big.label) + ["All other", "Latest 12 mo"]
    vals = [prior] + list(big.change) + [other, latest]
    fig = go.Figure(go.Waterfall(
        x=labels, y=vals, measure=["absolute"] + ["relative"] * (len(big) + 1) + ["total"],
        increasing=dict(marker=dict(color="#b91c1c")), decreasing=dict(marker=dict(color="#15803d")), totals=dict(marker=dict(color="#334155")),
        connector=dict(line=dict(color="#94a3b8", width=1)), text=[money(v) for v in vals], textposition="outside", cliponaxis=False,
        hovertemplate="%{x}: %{y:$,.0f}<extra></extra>"))
    lo, hi = min(prior, latest) * 0.8, max(prior, latest) * 1.12
    fig.update_layout(template=TEMPLATE, height=420, margin=dict(t=30, b=10), showlegend=False, yaxis=dict(tickformat="$.2~s", range=[lo, hi]),
                      xaxis=dict(tickangle=-35, tickfont=dict(size=9)))
    return fig


def movers_display(t: pd.DataFrame) -> pd.DataFrame:
    if t.empty:
        return t
    return pd.DataFrame({"Provider": t.provider, "Name": t.name.str.slice(0, 48), "Prior 12 mo": t.prior.map(money), "Latest 12 mo": t.latest.map(money),
                         "Change": t.change.map(money), "%": t.pct.map(lambda v: "new" if pd.isna(v) else (f"{1 + v:.0f}×" if v >= 3 else f"{v:+.0%}")), "Note": t.status})


def spikes_display(h: History) -> pd.DataFrame:
    if h.spikes is None or h.spikes.empty:
        return pd.DataFrame()
    s = h.spikes.sort_values("difference", key=lambda c: c.abs(), ascending=False)
    return pd.DataFrame({"Month": s.month.dt.strftime("%b %Y"), "Provider": s.provider, "Billed": s.billed.map(money), "Normal": s.normal.map(money),
                         "Difference": s.difference.map(money), "Main driver(s)": s.driver})


# --------------------------------------------------------------------------------------- Marketplace and tax (always separate)
def separate_summary(allc: pd.DataFrame, h: History) -> pd.DataFrame:
    rows = []
    for cat in ("Marketplace", "Tax"):
        d = allc[(allc.category == cat) & ~allc.is_partial.astype(bool)]
        s = d.groupby("date").cost_net.sum()
        c, pv = s.reindex(h.cur).sum(), (s.reindex(h.prev).sum() if h.has_prev else np.nan)
        rows.append({"Category": cat, "Prior 12 mo": money(pv), "Latest 12 mo": money(c),
                     "Change": pct(c / pv - 1) if pv == pv and pv else "–", f"FY{h.yr - 1}": money(s[s.index.year == h.yr - 1].sum())})
    return pd.DataFrame(rows)


def fig_marketplace(allc: pd.DataFrame, h: History) -> go.Figure:
    d = allc[(allc.category == "Marketplace") & ~allc.is_partial.astype(bool)]
    piv = d.pivot_table(index="date", columns="provider", values="cost_net", aggfunc="sum").reindex(h.m.index).fillna(0.0)
    fig = go.Figure()
    for p in piv.columns:
        fig.add_bar(x=piv.index, y=piv[p], name=f"{p} Marketplace", marker_color={"AWS": "#B45309", "GCP": "#7C3AED"}.get(p, "#64748b"),
                    hovertemplate="%{y:$,.0f}")
    fig.update_layout(template=TEMPLATE, barmode="stack", height=300, margin=dict(t=20, b=10), legend=dict(orientation="h", y=-0.18),
                      yaxis=dict(tickformat="$.2~s"), xaxis=dict(tickformat="%b %y", dtick="M2"), hovermode="x unified")
    return fig


# --------------------------------------------------------------------------------------- look-back test of the next-year method
def lookback(allc: pd.DataFrame, top_n: int = 4) -> dict | None:
    """Run the Next Year model as it would have run 12 months ago (data up to that month only, no reviewed adjustments)
    and compare its Low/Base/High with what was actually billed. Needs 24 billed months."""
    from . import nextyear as ny_mod
    from .schema import CLOUD
    billed = allc[~allc.is_partial.astype(bool)]
    last = billed.date.max()
    cut = last - pd.DateOffset(months=12)
    if billed.date.min() > cut - pd.DateOffset(months=11):
        return None
    ny = ny_mod.build(billed[billed.date <= cut], top_n, apply_adjustments=False)
    win = pd.date_range(cut + pd.DateOffset(months=1), last, freq="MS")
    out = {"cut": cut, "window": win, "providers": {}}
    svc = billed[billed.category == CLOUD]
    tot = {"Low": 0.0, "Base": 0.0, "High": 0.0, "Actual": 0.0}
    for p in ny_mod.PROVIDERS:
        act = svc[svc.provider == p].groupby("date").cost_net.sum().reindex(win).fillna(0.0)
        f = ny.svc[p].reindex(win)
        rec = {k: float(f[k].sum()) for k in ("Low", "Base", "High")}
        rec["Actual"] = float(act.sum())
        rec["series"] = pd.DataFrame({"Actual": act, "Low": f["Low"], "Base": f["Base"], "High": f["High"]})
        rec["error"] = rec["Base"] / rec["Actual"] - 1 if rec["Actual"] else np.nan
        rec["inside"] = rec["Low"] <= rec["Actual"] <= rec["High"]
        out["providers"][p] = rec
        for k in tot:
            tot[k] += rec[k]
    tot["error"] = tot["Base"] / tot["Actual"] - 1 if tot["Actual"] else np.nan
    tot["inside"] = tot["Low"] <= tot["Actual"] <= tot["High"]
    out["total"] = tot
    return out


def lookback_table(lb: dict) -> pd.DataFrame:
    rows = []
    for p, r in {**lb["providers"], "Total": lb["total"]}.items():
        rows.append({"": p, "Forecast Low": money(r["Low"]), "Forecast Base": money(r["Base"]), "Forecast High": money(r["High"]),
                     "Actually billed": money(r["Actual"]), "Base vs actual": pct(r["error"]), "Inside Low–High?": "Yes" if r["inside"] else "No"})
    return pd.DataFrame(rows)


def fig_lookback(lb: dict) -> go.Figure:
    provs = list(lb["providers"])
    fig = make_subplots(rows=1, cols=len(provs), subplot_titles=[f"{p} Cloud Services" for p in provs], horizontal_spacing=0.08)
    for ci, p in enumerate(provs, 1):
        s, col = lb["providers"][p]["series"], COLORS[p]
        h_ = col.lstrip("#")
        fill = f"rgba({int(h_[0:2], 16)},{int(h_[2:4], 16)},{int(h_[4:6], 16)},0.18)"
        fig.add_scatter(x=list(s.index) + list(s.index[::-1]), y=list(s.High) + list(s.Low[::-1]), fill="toself", fillcolor=fill,
                        line=dict(width=0), name="Forecast range", legendgroup="r", showlegend=(ci == 1), hoverinfo="skip", row=1, col=ci)
        fig.add_scatter(x=s.index, y=s.Base, mode="lines", line=dict(color=col, dash="dash", width=2), name="Forecast made 12 months earlier",
                        legendgroup="f", showlegend=(ci == 1), row=1, col=ci)
        fig.add_scatter(x=s.index, y=s.Actual, mode="lines+markers", line=dict(color="#0f172a", width=2.5), name="Actually billed",
                        legendgroup="a", showlegend=(ci == 1), row=1, col=ci)
    fig.update_yaxes(tickformat="$.2~s", rangemode="tozero")
    fig.update_xaxes(tickformat="%b %y", dtick="M3")
    fig.update_layout(template=TEMPLATE, height=330, margin=dict(t=40, b=10), legend=dict(orientation="h", y=-0.16))
    return fig


def lookback_text(lb: dict) -> list[str]:
    t = lb["total"]
    out = [f"Re-running the method with only the data available in **{lb['cut']:%b %Y}**, the Base forecast for the next 12 months was "
           f"**{money(t['Base'])}** (range {money(t['Low'])} – {money(t['High'])}); **{money(t['Actual'])}** was actually billed "
           f"({t['error']:+.1%}). The actual landed {'inside' if t['inside'] else 'outside'} the Low–High range."]
    for p, r in lb["providers"].items():
        out.append(f"{p}: forecast {money(r['Base'])} vs actual {money(r['Actual'])} ({r['error']:+.1%}); "
                   f"{'inside' if r['inside'] else 'outside'} its range.")
    return out


# --------------------------------------------------------------------------------------- executive summary in words
def _owner_of(h: History) -> dict:
    d = h.df[h.df.date.isin(h.cur)]
    if "owner" not in d:
        return {}
    g = d.groupby(["provider", "account", "owner"]).cost_net.sum().reset_index().sort_values("cost_net", ascending=False)
    return {(r.provider, r.account): r.owner for r in g.drop_duplicates(["provider", "account"]).itertuples() if r.owner != "(unassigned)"}


def _one_off_services(h: History) -> dict:
    """service name -> month of the up-spike it explains (only spikes that fall in the prior 12 months, i.e. did not repeat)."""
    out = {}
    if h.spikes is None or h.spikes.empty:
        return out
    for r in h.spikes[(h.spikes.direction == "Up") & h.spikes.month.isin(h.prev)].itertuples():
        for part in str(r.driver).split("; "):
            name = part.rsplit(" (", 1)[0].strip()
            if name:
                out[(r.provider, name)] = r.month
    return out


def _tag(r, one_off: dict) -> str:
    if r.prior < 1000:
        return "new"
    for (p, nm), mo in one_off.items():
        if p == r.provider and r.name.startswith(nm[:30]) and r.change < 0:
            return f"one-off in {mo:%b %Y}"
    if r.latest < 0.1 * r.prior:
        return "ended"
    return "reduced" if r.change < 0 else "grew"


def story(h: History, level: str, n: int = 5) -> list[str]:
    """Plain-language summary of what moved between the prior 12 months and the latest 12, at `level` ('service' | 'account')."""
    if not h.has_prev:
        return []
    t = movers(h, level, 100_000)
    if t.empty:
        return []
    noun = {"service": "service", "account": "account / project"}[level]
    plural = {"service": "services", "account": "accounts / projects"}[level]
    sg = lambda v: ("+" if v > 0 else "") + money(v)
    owners = _owner_of(h) if level == "account" else {}
    one_off = _one_off_services(h) if level == "service" else {}
    t = t.assign(tag=[_tag(r, one_off) for r in t.itertuples()])
    t["name"] = t["name"].replace({"(no project)": "(no project: Google Support / third-party)"}) if level == "account" else t["name"]
    dn, up = t[t.change < 0], t[t.change > 0]
    net, tot_dn, tot_up = t.change.sum(), -dn.change.sum(), up.change.sum()
    out = [f"**Net change: {sg(net)}** ({money(h.m.loc[h.prev, 'Total'].sum())} → {money(h.m.loc[h.cur, 'Total'].sum())}). "
           f"Spend fell by **{money(tot_dn)}** across {len(dn)} {plural} and rose by **{money(tot_up)}** across {len(up)}."]
    prov = []
    for p in h.provs:
        a, b = dn[dn.provider == p].change.sum(), up[up.provider == p].change.sum()
        prov.append(f"{p}: {money(-a)} down, {money(b)} up (net {sg(a + b)})")
    out.append("By provider — " + "; ".join(prov) + ".")
    nets = {p: dn[dn.provider == p].change.sum() + up[up.provider == p].change.sum() for p in h.provs}
    if len(nets) == 2 and min(nets.values()) < 0 < max(nets.values()):
        out.append("One provider fell while the other rose, so part of the 'reduction' may be workloads that **moved between clouds** rather than savings; "
                   "the net across both is the number to hold the budget against.")

    def line(r, owners=owners):
        own = owners.get((r.provider, r.name)) if level == "account" else None
        pct_ = "new" if r.prior < 1000 else (f"{1 + r.pct:.0f}×" if r.pct >= 3 else f"{r.pct:+.0%}")
        what = {"ended": "ended or near zero", "reduced": "reduced", "grew": "grew", "new": ""}.get(
            r.tag, f"{r.tag}, not a saving" if r.tag.startswith("one-off") else r.tag)
        return (f"**{r.provider} · {r.name[:46]}**: {money(r.prior)} → {money(r.latest)} ({pct_}{', ' + what if what else ''})"
                + (f" — owner {own}" if own else ""))

    # --- savings
    by_tag = dn.groupby("tag").change.sum().abs()
    split = []
    for k, label in (("reduced", "lines that shrank"), ("ended", "lines that ended"), ):
        if by_tag.get(k, 0) > 0:
            split.append(f"{money(by_tag[k])} from {label}")
    for k in by_tag.index:
        if k.startswith("one-off"):
            split.append(f"{money(by_tag[k])} from a one-off that did not repeat ({k.replace('one-off in ', '')})")
    out.append(f"### Where spend went down ({money(tot_dn)})")
    if split:
        out.append("Of the reduction: " + ", ".join(split) + ". Only the shrinkage is a saving from running things smaller; "
                   "ended lines are shutdowns (confirm they were intended) and a one-off was never recurring cost.")
    top_dn = dn.sort_values("change").head(n)
    out += [f"- {line(r)}" for r in top_dn.itertuples()]
    if len(dn) > n and tot_dn:
        out.append(f"- The {n} largest reductions are {top_dn.change.abs().sum() / tot_dn:.0%} of the total; the rest is spread over {len(dn) - n} smaller {plural}.")

    # --- increases
    new_tot, grow_tot = up[up.tag == "new"].change.sum(), up[up.tag != "new"].change.sum()
    out.append(f"### Where spend went up ({money(tot_up)})")
    out.append(f"Of the increase: {money(new_tot)} is from {int((up.tag == 'new').sum())} {plural} that did not exist or were under $1K before, "
               f"and {money(grow_tot)} is growth in {plural} that already had spend.")
    top_up = up.sort_values("change", ascending=False).head(n)
    out += [f"- {line(r)}" for r in top_up.itertuples()]
    if len(up) > n and tot_up:
        out.append(f"- The {n} largest increases are {top_up.change.sum() / tot_up:.0%} of the total.")
    return out


def story_sections(h: History, n: int = 5) -> list[tuple[str, list[str]]]:
    return [("By service", story(h, "service", n)), ("By account and project", story(h, "account", n))]
