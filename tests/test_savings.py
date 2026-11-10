import numpy as np
import pandas as pd
import pytest

from finops import savings as S
from tests import synth

IDX = pd.date_range("2024-09-01", periods=27, freq="MS")


def line(values):
    return S._scan_line(np.asarray(values, dtype=float), IDX, S.MATERIALITY)


def test_step_down_is_a_reduced_saving_with_exact_realized():
    r = line([50_000] * 12 + [20_000] * 15)
    assert r["cls"] == "Reduced"
    assert r["peak"] == 50_000 and r["now"] == 20_000 and r["lower"] == 30_000
    assert r["since"] == IDX[12]                      # first month at the new level
    assert r["realized"] == 15 * 30_000


def test_ended_line():
    r = line([15_000] * 14 + [0] * 13)
    assert r["cls"] == "Ended"
    assert r["realized"] == 13 * 15_000


def test_single_spike_is_a_one_off_and_realizes_nothing():
    v = [10_000] * 27
    v[6] = 200_000
    r = line(v)
    assert r["cls"] == "One-off" and r["realized"] == 0.0


def test_partial_zero_months_look_like_a_billing_gap_not_a_saving():
    r = line([20_000] * 24 + [10_000, 0, 10_000])
    assert r["cls"] == "Check" and r["realized"] == 0.0


@pytest.mark.parametrize("values", [
    [30_000] * 27,                                   # flat
    [3_000] * 12 + [1_000] * 15,                     # below materiality
    [50_000] * 24 + [20_000, 50_000, 50_000],        # a dip that is not sustained
])
def test_no_ledger_entry(values):
    r = line(values)
    assert r is None or "cls" not in r


def test_a_small_percentage_fall_is_not_a_saving_even_if_the_dollars_are_material():
    r = line([100_000] * 12 + [90_000] * 15)          # $10K/month lower, but only 10% below its peak (needs >= 15%)
    assert r is None or "cls" not in r
    r = line([100_000] * 12 + [84_000] * 15)
    assert r["cls"] == "Reduced"


def test_materiality_is_respected():
    assert "cls" not in (line([40_000] * 12 + [36_000] * 15) or {})        # $4K < $5K
    assert line([40_000] * 12 + [20_000] * 15)["cls"] == "Reduced"


def test_too_little_history_is_ignored():
    assert line([50_000] * 5 + [10_000] * 3) is None


def test_erosion_share_given_back():
    r = line([50_000] * 6 + [20_000] * 8 + [40_000] * 13)
    e = r["erosion"]
    assert e["trough"] == 20_000
    assert e["given_back"] == pytest.approx((40_000 - 20_000) / (50_000 - 20_000))


def test_scan_on_synthetic_data(cloud):
    sc = S.scan(S.series(cloud, "account"), owners=S.owners_map(cloud, "account"))
    cls = dict(zip(sc.ledger.name, sc.ledger.cls))
    assert cls["Acct-Shrunk"] == "Reduced" and cls["Acct-Ended"] == "Ended" and cls["Acct-Spike"] == "One-off"
    assert "Acct-Steady" not in cls and "prj-ramp" not in cls
    assert sc.ledger.set_index("name").loc["Acct-Shrunk", "owner"] == "Alice"
    # the monthly chart data must add up to the ledger
    L = sc.ledger
    assert sc.avoided["Reduced"].sum() == pytest.approx(L[L.cls == "Reduced"].realized.sum())
    assert sc.avoided["Ended"].sum() == pytest.approx(L[L.cls == "Ended"].realized.sum())


def test_savings_plans_service_is_skipped(cloud):
    sc = S.scan(S.series(cloud, "service"))
    assert S.SP_NAME if hasattr(S, "SP_NAME") else True
    assert "Savings Plans for AWS Compute usage" not in set(sc.ledger.name)


def test_owner_scorecard_adds_up(cloud):
    from finops import history as H
    h = H.compute(cloud)
    acct = S.scan(S.series(cloud, "account"), owners=S.owners_map(cloud, "account"))
    t = S.owner_scorecard(cloud, h, acct)
    assert t.latest.sum() == pytest.approx(h.m.loc[h.cur, "Total"].sum())
    assert t.prior.sum() == pytest.approx(h.m.loc[h.prev, "Total"].sum())
    assert t.loc["Alice", "saving_mo"] == pytest.approx(20_000)


def test_displays_do_not_crash(cloud):
    from finops import history as H
    h = H.compute(cloud)
    acct = S.scan(S.series(cloud, "account"), owners=S.owners_map(cloud, "account"))
    S.ledger_display(acct.ledger), S.erosion_display(acct.erosion), S.kpis(acct, h), S.narrative(acct, h, "account")
    S.arc_lines(cloud)
    for f in (S.fig_over_time, S.fig_top):
        f(acct)
    S.fig_arc(cloud)
    t = S.owner_scorecard(cloud, h, acct)
    S.scorecard_display(t), S.fig_owner_change(t), S.owner_detail(cloud, h, "Alice", acct)


# ------------------------------------------------------------------ billing gaps must not look like savings
RULE_COLS = ["provider", "account", "service", "months", "action", "note"]


def _line_df(values, account="Acct-Gap", service="Amazon Relational Database Service"):
    return pd.DataFrame([dict(date=d, provider="AWS", account=account, service=service, category="Cloud Services", env="unknown", cost_gross=v, credits=0.0,
                              cost_net=v, owner="Alice", family="g", is_partial=False) for d, v in zip(IDX, values)])


def test_local_gap_fills_from_neighbours():
    v = pd.Series([100.0] * 10, index=IDX[:10])
    v.iloc[4:6] = 0.0
    g = S.local_gap(v, [IDX[4], IDX[5]])
    assert g.iloc[4] == 100 and g.iloc[5] == 100 and g.sum() == 200                 # neighbours are non-gap months only
    auto = S.local_gap(v, None)
    assert auto.sum() == 200                                                        # detected without being told


def test_a_billing_gap_reads_as_savings_coming_back_until_it_is_reviewed():
    values = [30_000.0] * 27
    values[17] = values[18] = 0.0
    df = _line_df(values)
    raw = S.scan(S.series(df, "account"))
    assert len(raw.erosion) == 1 and raw.erosion.iloc[0].given_back == pytest.approx(1.0)          # the false signal
    rule = pd.DataFrame([dict(provider="AWS", account="Acct-Gap", service="Amazon Relational Database Service",
                              months=f"{IDX[17]:%Y-%m};{IDX[18]:%Y-%m}", action="add_back", note="")])
    fixed = S.scan(S.adjusted_series(df, "account", rule))
    assert fixed.erosion.empty and fixed.ledger.empty
    only_pending = rule.assign(action="pending")
    assert len(S.scan(S.adjusted_series(df, "account", only_pending)).erosion) == 1               # only add_back rows adjust anything


def test_savings_plan_fee_timing_gaps_are_added_back_at_account_level(cloud):
    empty = pd.DataFrame(columns=RULE_COLS)
    raw = S.series(cloud, "account")[("AWS", "Acct-Steady")]
    adj = S.adjusted_series(cloud, "account", empty)[("AWS", "Acct-Steady")]
    assert raw.min() == 50_000 and adj.min() == pytest.approx(55_000) and adj.max() == pytest.approx(55_000)


def test_provider_totals_are_gap_adjusted_too(cloud):
    empty = pd.DataFrame(columns=RULE_COLS)
    raw, adj = S.total_series(cloud)[("AWS", "All")], S.adjusted_totals(cloud, empty)[("AWS", "All")]
    assert adj.sum() == pytest.approx(raw.sum() + 3 * 5_000)                       # three missing $5K Savings Plans bills
