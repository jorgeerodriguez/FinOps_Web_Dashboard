"""Next Year Forecast: numbers, tables and text (no Streamlit). Shared by views/next_year.py and the HTML report."""
from __future__ import annotations
import numpy as np
import pandas as pd

from . import nextyear as ny_mod
from .nextyear import NextYear
from .ui import cfg, money, pct

PROV = ("AWS", "GCP")


def default_budgets(fy: int) -> tuple[dict, bool]:
    """Monthly budgets for the forecast year from config; falls back to the prior year (flagged as assumed)."""
    cur = cfg().get("budget", {}).get(str(fy), {})
    prev = cfg().get("budget", {}).get(str(fy - 1), {})
    assumed = not (cur.get("aws_monthly") or cur.get("gcp_monthly"))
    return {"AWS": int(cur.get("aws_monthly", 0) or prev.get("aws_monthly", 0)),
            "GCP": int(cur.get("gcp_monthly", 0) or prev.get("gcp_monthly", 0))}, assumed


def summarize(ny: NextYear, budgets: dict) -> dict:
    m = ny.months
    S = {p: {sc: ny.svc[p].loc[m, sc].sum() for sc in ny_mod.SCEN} for p in PROV}
    mk_fy = ny.mkt.loc[m].sum()
    tot = {sc: S["AWS"][sc] + S["GCP"][sc] for sc in ny_mod.SCEN}
    fy_bud = {p: budgets[p] * 12 for p in PROV}
    return dict(fy=ny.fy, m=m, S=S, fy_bud=fy_bud, tot=tot, tot_bud=sum(fy_bud.values()), mk_fy=mk_fy,
                cy=sum(ny.current_year.values()),
                tax_est=sum(ny.tax_rate[p] * (S[p]["Base"] + mk_fy[p]) for p in PROV))


def kpi_items(ny: NextYear, sm: dict) -> list[dict]:
    tot, tb, cy, fy = sm["tot"], sm["tot_bud"], sm["cy"], sm["fy"]
    return [
        dict(label=f"FY{fy} Cloud Services (base)", value=money(tot["Base"]), neutral=True,
             delta=(pct(tot["Base"] / cy - 1) + f" vs {ny.last.year} est.") if cy else "",
             help=f"{ny.last.year} estimate = billed so far + base forecast for the rest of the year ({money(cy)})"),
        dict(label="Range (Low – High)", value=f"{money(tot['Low'])} – {money(tot['High'])}", delta="", help=""),
        dict(label="vs FY budget (base)", value=(money(tb - tot["Base"]) + (" under" if tb >= tot["Base"] else " over")) if tb else "n/a",
             delta=pct(tot["Base"] / tb - 1) if tb else "", inverse=True, help=f"FY budget {money(tb)}"),
        dict(label=f"Marketplace FY{fy} (scheduled)", value=money(sm["mk_fy"].sum()), delta="", neutral=True,
             help="From the contract renewal calendar; outside the budget"),
        dict(label="Est. tax (separate)", value=money(sm["tax_est"]), delta="", neutral=True,
             help="Each provider's observed tax rate × (services + marketplace)"),
    ]


def bullets(ny: NextYear, sm: dict) -> list[str]:
    S, fb, fy, mk, tot = sm["S"], sm["fy_bud"], sm["fy"], sm["mk_fy"], sm["tot"]
    out = []
    for p in PROV:
        pr = ny.params[p]
        over = fb[p] and S[p]["Base"] > fb[p]
        out.append(f"**{p} Cloud Services:** base **{money(S[p]['Base'])}** (range {money(S[p]['Low'])}–{money(S[p]['High'])}); "
                   f"exit run-rate {money(pr['anchor'])}/mo, trend {'+' if pr['slope'] >= 0 else '−'}{money(abs(pr['slope']))}/mo per month"
                   + (f"; budget {money(fb[p])} → **{money(abs(fb[p] - S[p]['Base']))} {'over' if over else 'under'}** at base"
                      + (" (even the Low case is over budget)" if S[p]["Low"] > fb[p] else
                         " (the Low case is within budget)" if over else "") if fb[p] else ""))
    out.append(f"**Marketplace:** {money(mk.sum())} scheduled in FY{fy} (AWS {money(mk['AWS'])}, GCP {money(mk['GCP'])}) — planned by renewal, not trend.")
    out.append(f"**All-in (Services base + Marketplace):** {money(tot['Base'] + mk.sum())}, before an estimated {money(sm['tax_est'])} of tax.")
    one = adjustments_oneliner(ny)
    return out + ([one] if one else [])


def fy_summary_df(ny: NextYear, sm: dict) -> pd.DataFrame:
    S, fb, ly = sm["S"], sm["fy_bud"], ny.last.year
    rows = [{"": p, f"{ly} est.": ny.current_year[p], "Low": S[p]["Low"], "Base": S[p]["Base"], "High": S[p]["High"],
             "Budget": fb[p] or np.nan, "Var. (Base)": (fb[p] - S[p]["Base"]) if fb[p] else np.nan} for p in PROV]
    t = sm["tot"]
    rows.append({"": "Total", f"{ly} est.": sm["cy"], "Low": t["Low"], "Base": t["Base"], "High": t["High"],
                 "Budget": sm["tot_bud"] or np.nan, "Var. (Base)": (sm["tot_bud"] - t["Base"]) if sm["tot_bud"] else np.nan})
    return pd.DataFrame(rows).set_index("")


def quarterly_df(ny: NextYear, budgets: dict) -> pd.DataFrame:
    idx, rows = ny.months, {}
    for p in PROV:
        for sc in ny_mod.SCEN:
            rows[f"{p} — {sc}"] = ny.svc[p].loc[idx, sc]
        rows[f"{p} — Budget"] = pd.Series(budgets[p], index=idx)
        rows[f"{p} — Variance (Base)"] = rows[f"{p} — Budget"] - rows[f"{p} — Base"]
    for sc in ny_mod.SCEN:
        rows[f"Total — {sc}"] = ny.svc["AWS"].loc[idx, sc] + ny.svc["GCP"].loc[idx, sc]
    rows["Total — Budget"] = pd.Series(budgets["AWS"] + budgets["GCP"], index=idx)
    rows["Total — Variance (Base)"] = rows["Total — Budget"] - rows["Total — Base"]
    q = pd.DataFrame(rows).groupby(pd.DataFrame(rows).index.quarter).sum()
    q.index = [f"Q{i}" for i in q.index]
    q.loc[f"FY{ny.fy}"] = q.sum()
    return q.T


def monthly_df(ny: NextYear, budgets: dict, view: str = "Services", scenario: str = "Base") -> pd.DataFrame:
    idx = ny.months
    mt = pd.DataFrame(index=idx)
    if view in ("Services", "Combined"):
        for p in PROV:
            mt[f"{p} Services"] = ny.svc[p].loc[idx, scenario]
    if view in ("Marketplace", "Combined"):
        for p in PROV:
            mt[f"{p} Marketplace"] = ny.mkt.loc[idx, p]
    mt["Total"] = mt.sum(axis=1)
    if view == "Services":
        mt["Budget"] = budgets["AWS"] + budgets["GCP"]
        mt["Variance"] = mt["Budget"] - mt["Total"]
    mt.loc["FY" + str(ny.fy)] = mt.sum()
    mt.index = [i.strftime("%b %Y") if isinstance(i, pd.Timestamp) else i for i in mt.index]
    return mt


def marketplace_quarter_df(ny: NextYear) -> pd.DataFrame:
    mq = ny.mkt.loc[ny.months].copy()
    mq["Total"] = mq.sum(axis=1)
    mq = mq.groupby(mq.index.quarter).sum()
    mq.index = [f"Q{i} {ny.fy}" for i in mq.index]
    mq.loc[f"FY{ny.fy}"] = mq.sum()
    return mq


def top_contracts_display(ny: NextYear, n: int = 10) -> pd.DataFrame | None:
    if not len(ny.contracts):
        return None
    ct = ny.contracts
    ct = ct[ct["cadence"] != "Not billed in last 12 mo"].sort_values("amount", ascending=False).head(n)
    return (ct.assign(amount=ct.amount.map(money), billed_12mo=ct.billed_12mo.map(money), next_charge=ct.next_charge.dt.strftime("%b %Y").fillna("–"))
            [["provider", "vendor", "cadence", "amount", "billed_12mo", "next_charge"]].rename(columns={"amount": "per charge", "billed_12mo": "billed 12 mo"}))


def drivers_display(ny: NextYear, p: str, n: int = 8) -> pd.DataFrame:
    d = ny.drivers[p].head(n)
    return d.assign(**{"Avg / mo": d["Avg / mo"].map(money), "Exit run-rate": d["Exit run-rate"].map(money),
                       "Trend $/mo per month": d["Trend $/mo per month"].map(lambda v: ("+" if v > 0 else "") + money(v)),
                       "Share of growth": d["Share of growth"].map(lambda v: "–" if pd.isna(v) else f"{v:.0%}")})


def account_text(ny: NextYear, sm: dict, p: str) -> str:
    t, fyc = ny.tables[p], f"FY{ny.fy} forecast"
    td_, bu_ = sm["S"][p]["Base"], t[fyc].sum()
    return (f"Top-down {p} base **{money(td_)}** vs bottom-up (sum of lines) **{money(bu_)}** — {pct(bu_ / td_ - 1)} difference. "
            + ("Close: the two methods agree." if abs(bu_ / td_ - 1) < 0.08 else
               "A gap this size means some lines' models extrapolate harder than the provider total — review the lines marked Low confidence."))


def account_display(ny: NextYear, p: str) -> pd.DataFrame:
    t, fyc = ny.tables[p], f"FY{ny.fy} forecast"
    return t.assign(**{c: t[c].map(money) for c in ["Last 12 mo", "Exit run-rate /mo", fyc, "Low", "High"]},
                    **{"Δ vs exit × 12": t["Δ vs exit × 12"].map(lambda v: ("+" if v > 0 else "") + money(v))}, Share=t["Share"].map("{:.0%}".format))


def adjustments_oneliner(ny: NextYear) -> str | None:
    """One compact bullet for the summary; the full per-provider detail is in the anomalies section."""
    if not ny.adjusted:
        return "**Anomaly adjustments are OFF** — raw model (no add-backs, 12-month trend)."
    parts = []
    for p in PROV:
        e = ny.effects.get(p)
        if e and (e["add_back"] > 0 or e["trend_since"] is not None):
            why = "billing gaps added back" if e["add_back"] > 0 else f"trend since {pd.Timestamp(e['trend_since']):%b %Y}"
            parts.append(f"{p} {money(e['raw']['Base'])} → {money(e['adjusted']['Base'])} ({why})")
    return ("**Reviewed adjustments** (FY%d base): " % ny.fy + "; ".join(parts) + ".") if parts else None


def adjustments_lines(ny: NextYear) -> list[str]:
    """What the reviewed adjustments did to the forecast (empty when they are switched off)."""
    if not ny.adjusted:
        return ["**Anomaly adjustments are OFF** — the numbers below are the raw model (no add-backs, 12-month trend)."]
    out = []
    for p in PROV:
        e = ny.effects.get(p)
        if not e:
            continue
        bits = []
        if e["add_back"] > 0:
            bits.append(f"{money(e['add_back'])} of reviewed billing gaps added back to the trend (plus the automatic Savings Plans correction)")
        if e["trend_since"] is not None:
            bits.append(f"trend measured since {pd.Timestamp(e['trend_since']):%b %Y} to skip the launch ramp")
        if bits:
            out.append(f"**{p}:** " + "; ".join(bits) + f" — FY{ny.fy} base {money(e['raw']['Base'])} → **{money(e['adjusted']['Base'])}**.")
    an = ny.anomalies
    if an is not None and len(an):
        pend = an[an["Status"] == "Pending review"]
        if len(pend):
            out.append(f"**{len(pend)} anomalies still pending review** ({'; '.join(f'{r.Provider} {r._2} {r.Service}' for r in pend.itertuples())[:260]}).")
    return out


def anomalies_display(ny: NextYear) -> pd.DataFrame | None:
    an = ny.anomalies
    if an is None or not len(an):
        return None
    an = an.assign(_p=an["Status"].ne("Pending review")).sort_values(["_p", "Missing vs typical"], ascending=[True, False]).drop(columns=["_p", "_service", "_months"], errors="ignore")
    return an.assign(**{"Typical /mo": an["Typical /mo"].map(money), "Missing vs typical": an["Missing vs typical"].map(money)})


ANOMALY_CAPTION = ("Lines that normally cost ≥ $3K/month but show near $0 in some months, classified as **Gap** (billing timing), **New workload** "
                   "(starts after the window began) or **Shutdown / fading** (ends before the window ends). Gaps distort the trend and are added back once "
                   "reviewed; new workloads and shutdowns are real. Edit `data/anomaly_rules.csv` to record a review.")

ACCOUNT_CAPTION = ("Each account / project competes four models on a backtest (Flat, Damped, Linear, Holt); the simplest within 5% of the best wins. "
                   "Forecasts are capped at 2× and floored at 0.5× the exit run-rate.")

BU_CAPTION = ("Cut the cloud bill by account, project code, owner, family, environment … (fields from your owner files), pick a model and a training window. "
              "Every group is forecast on its own and the groups add up to the provider total: spend with no value for the field is its own “(not set)” line, "
              "the smallest groups are summed into “All other”. Default = Account / project, Auto, last 12 months (unchanged).")

METHOD_MD = """
**Cloud Services (per provider).** Start = average of the last 3 billed months (exit run-rate). Trend = Theil-Sen slope of the last 12 billed
months (median of all pairwise slopes, so one-off months don't distort it). *Low* = flat; *Base* = damped trend (momentum fades 15% each month);
*High* = trend continues linearly. AWS Savings-Plan-fee gaps are added back for the trend only.

**Bottom-up by any grouping.** The Cloud Services bill is cut by the field you choose (account / project, project code, owner, account family,
environment, service name, lifecycle status, cloud service, cost center, provider × environment) and every group is forecast on its own, then the
groups are added up. Spend with no value for the field is kept as its own “(not set)” line and the smallest groups are summed into “All other”, so the
groups always add up to the provider total. Savings-Plan fee gaps and reviewed add-backs are placed on the group that carries them.
*Model*: **Auto** (default) backtests the models per group and keeps the simplest within 5% of the most accurate; or choose one model for every group:
Flat, Damped trend, Linear, Holt (ETS), Ensemble (average of Flat, Damped and Holt), Seasonal naive × YoY, Seasonal profile, Holt-Winters.
Every model is backtested on the same months (rolling origin, up to 3 months ahead); seasonal models are scored only when enough history exists.
Confidence = backtest error as a % of the exit run-rate (High < 10%, Medium < 25%); range = ±1.28 × RMSE, widening with horizon; forecasts are capped at
2× and floored at 0.5× the exit run-rate. *Training window*: 12 / 18 / 24 months or all billed months (the top-down forecast always uses 12).
*Reconcile to top-down* scales every group by one factor so they add up to the top-down Base; each group keeps its share.

**Marketplace.** Each contract repeats the billing cadence seen in the last 12 months (monthly, quarterly, annual …). Outside the Cloud Services budget.

**Tax.** Observed tax rate per provider applied to Services + Marketplace; shown separately.

**Reviewed adjustments.** Billing gaps confirmed as timing (data/anomaly_rules.csv) are added back for the trend only; the Savings Plans gap is
corrected automatically; GCP's trend is measured only from the month set in config (skipping a one-time launch ramp). New workloads and
shutdowns are real and are not adjusted.

**Look-back.** The method is re-run as it would have run 12 months ago (data up to that month only, no reviewed adjustments) and compared with what
was billed, so the ranges can be judged against a real result. The earlier months of history are drawn grey in the provider charts for context.

**Caveats.** The top-down forecast and the default bottom-up use the latest 12 billed months. Two years of history is enough to show last year's pattern and
to test the method, but not to estimate seasonality reliably (it is confused with growth): the seasonal models are offered, scored honestly on the leaderboard,
and not used by Auto unless they win on the backtest. Longer training windows include GCP's launch ramp (Nov 2025 – Jan 2026).
Annual Marketplace renewals assume the same price. New workloads or planned savings are not in the baseline — use the Forecast & Budget page's
adjustment slider to layer them on.
"""


# ------------------------------------------------------------------------------------------ bottom-up by any grouping
def bu_text(bu, fmt_money=None) -> str:
    """One paragraph above the table: how the groups compare with the top-down forecast, and how complete the grouping is."""
    f = fmt_money or money
    if bu.table is None or not len(bu.table):
        return "No spend in this scope."
    diff = bu.total / bu.topdown - 1 if bu.topdown else float("nan")
    out = (f"Top-down {bu.scope} base **{f(bu.topdown)}** vs bottom-up by {bu.label.lower()} **{f(bu.total)}**"
           + (f" — {pct(diff)} difference. " if bu.scale == 1.0 else " (reconciled to the top-down, so they match by construction). "))
    if bu.scale == 1.0 and np.isfinite(diff):
        out += ("Close: the two methods agree. " if abs(diff) < 0.08 else
                "A gap this size means some groups' models extrapolate harder than the provider total (usually a few large groups each carrying the trend) — "
                "review the Low-confidence lines, or tick *Reconcile to top-down*. ")
    if bu.not_set and bu.not_set_share > 0:
        out += f"**{bu.not_set_share:.0%}** of the last 12 months' spend has no value for this field and sits in its own line (“{bu.not_set}”)."
    return out


def bu_display(bu) -> pd.DataFrame:
    t, fyc = bu.table, f"FY{bu.fy} forecast"
    out = t.assign(**{c: t[c].map(money) for c in ["Last 12 mo", "Exit run-rate /mo", fyc, "Low", "High"]},
                   **{"Δ vs exit × 12": t["Δ vs exit × 12"].map(lambda v: ("+" if v > 0 else "") + money(v))}, Share=t["Share"].map("{:.0%}".format))
    return out


def board_display(bu) -> pd.DataFrame:
    """Backtest error (MAE / exit run-rate) per line and model. Lower is better; blank = that model could not be tested on the line."""
    b = bu.board
    if b is None or b.empty:
        return pd.DataFrame()
    num = [c for c in b.columns if c != "Used"]
    out = b.copy()
    for c in num:
        out[c] = b[c].map(lambda v: "" if pd.isna(v) else f"{v:.1%}")
    out = out[num + ["Used"]]
    n = b.attrs.get("scored", {})
    out.loc["Lines tested"] = [f"{n.get(c, 0)} of {b.attrs.get('lines', 0)}" for c in num] + [""]
    return out


def model_verdict(bu) -> str:
    """Plain-language reading of the spend-weighted leaderboard row."""
    b = bu.board
    if b is None or b.empty:
        return ""
    w = b.loc["All lines (spend-weighted)"].drop("Used").astype(float).dropna()
    n = b.attrs.get("scored", {})
    full = [m for m in w.index if n.get(m, 0) == b.attrs.get("lines", 0)]            # compare only models tested on every line
    if not full:
        return ""
    best = min(full, key=w.get)
    used = sum(lf.scores.get(lf.model, np.nan) for lf in bu.lines if np.isfinite(lf.scores.get(lf.model, np.nan)))
    anchor = sum(lf.actual.tail(3).mean() for lf in bu.lines if np.isfinite(lf.scores.get(lf.model, np.nan)))
    mine = used / anchor if anchor else np.nan
    txt = f"Backtest over {bu.lines[0].origins} months (spend-weighted): lowest error **{best}** at {w[best]:.1%}"
    if bu.model == "Auto" and np.isfinite(mine):
        txt += f"; the Auto mix scored {mine:.1%}"
    elif bu.model != "Auto" and bu.model in w.index:
        txt += f"; your selection **{bu.model}** scored {w[bu.model]:.1%}"
    return txt + ". Error = average miss per month as a share of the exit run-rate; every model is scored on the same months."
