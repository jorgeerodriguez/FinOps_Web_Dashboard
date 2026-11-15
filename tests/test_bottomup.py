"""Bottom-up forecast by any grouping + the selectable models (finops/models.py, finops/bottomup.py)."""
from __future__ import annotations
import numpy as np
import pandas as pd
import pytest

from finops import bottomup as B
from finops import models as M
from finops import nextyear as N

ENV = {"Acct-Steady": "prod", "Acct-Shrunk": "non-prod", "Acct-Ended": "prod", "Acct-Spike": "non-prod", "prj-ramp": "prod", "prj-small": "unspecified"}
CODE = {"Acct-Steady": "Core", "Acct-Shrunk": "Core", "Acct-Ended": "Media", "Acct-Spike": "Media", "prj-ramp": "Core", "prj-small": "(no project code)"}


def series(vals, start="2024-09-01"):
    return pd.Series(vals, index=pd.date_range(start, periods=len(vals), freq="MS"), dtype=float)


def seasonal(n=30, amp=20.0, level=100.0, slope=0.0, noise=0.0, seed=0):
    t = np.arange(n)
    rng = np.random.default_rng(seed)
    return series(level + slope * t + amp * np.sin(2 * np.pi * (t % 12) / 12) + rng.normal(0, noise, n))


@pytest.fixture
def df(billing):
    d = billing.copy()
    d["environment"] = d.account.map(ENV)
    d["project_code"] = d.account.map(CODE)
    d["service_name"] = "(no service name)"
    d["cost_center"] = "(no cost center)"
    d["status"] = "Active"
    return d


@pytest.fixture
def ny(df):
    return N.build(df[~df.is_partial], 6, False)


# ------------------------------------------------------------------------------------------ models
def test_every_model_returns_finite_months_of_the_right_length():
    s = seasonal(30, noise=2.0)
    for m in M.MODELS:
        out = M.predict(m, s, 14)
        assert out.shape == (14,) and np.isfinite(out).all(), m


def test_flat_damped_linear_ensemble_arithmetic():
    s = series(np.arange(100.0, 112.0))                      # +1 a month, last 3 average 110
    assert M.predict("Flat", s, 3).tolist() == [110.0] * 3
    lin = M.predict("Linear", s, 3)
    assert lin == pytest.approx([111, 112, 113])
    d = M.predict("Damped trend", s, 3)
    assert d[0] == pytest.approx(110 + 0.85) and (d < lin).all() and (d > 110).all()
    ens = M.predict("Ensemble", s, 3)
    assert ens == pytest.approx(np.mean([M.predict(c, s, 3) for c in M.ENSEMBLE_OF], axis=0))


def test_seasonal_naive_repeats_last_years_shape_with_growth():
    pat = [100.0 + 10 * m for m in range(12)]
    truth = [pat[t % 12] * 1.10 ** (t // 12) for t in range(44)]            # a seasonal shape growing exactly 10% a year
    out = M.predict("Seasonal naive × YoY", series(truth[:30]), 14)
    assert out == pytest.approx(truth[30:44], rel=1e-9)                     # incl. months 13-14 out, which carry two years of growth


def test_seasonal_profile_and_holt_winters_see_a_pure_seasonal_series():
    s = seasonal(30, amp=30.0)
    truth = seasonal(30 + 6, amp=30.0).iloc[-6:].to_numpy()
    flat_err = np.abs(M.predict("Flat", s, 6) - truth).mean()
    for m in ("Seasonal profile", "Holt-Winters (seasonal)", "Seasonal naive × YoY"):
        assert np.abs(M.predict(m, s, 6) - truth).mean() < flat_err, m


def test_models_refuse_too_little_history_instead_of_guessing():
    s = series(np.linspace(100, 120, 20))
    assert M.can_fit("Seasonal profile", s) and not M.can_fit("Holt-Winters (seasonal)", s)
    with pytest.raises(ValueError):
        M.predict("Holt-Winters (seasonal)", s, 3)
    with pytest.raises(ValueError):
        M.predict("Seasonal naive × YoY", s.iloc[:15], 3)


def test_seasonal_models_count_history_from_the_first_non_zero_month():
    s = series([0.0] * 10 + list(np.linspace(100, 130, 14)))                # 24 months, only 14 of them real
    assert M.active_months(s) == 14 and not M.can_fit("Seasonal profile", s)


def test_every_model_is_scored_on_the_same_months():
    sc = M.score(seasonal(26, noise=3.0, slope=1.0))
    assert sc["origins"] == 26 - 18 and set(sc["table"]) == {"Flat", "Damped trend", "Linear", "Holt (ETS)", "Ensemble", "Seasonal naive × YoY", "Seasonal profile"}
    short = M.score(series(np.linspace(100, 130, 12)))                        # 12 months: only the non-seasonal models, from month 5
    assert short["origins"] == 7 and not (set(short["table"]) & M.SEASONAL)
    long = M.score(seasonal(32, noise=3.0))
    assert "Holt-Winters (seasonal)" in long["table"] and long["origins"] == 32 - 24


# ------------------------------------------------------------------------------------------ Auto is unchanged
def _legacy_line(actual: pd.Series, horizon: pd.DatetimeIndex):
    """The pre-refactor selection, verbatim: four models, backtest from month 5, simplest within 5% of the best MAE."""
    y = actual.to_numpy(float)
    scores = {}
    for m in N.MODELS:
        try:
            e = N.backtest(y, m)
            if len(e):
                scores[m] = dict(mae=float(np.mean(np.abs(e))), rmse=float(np.sqrt(np.mean(e ** 2))))
        except Exception:
            continue
    best = np.nanmin([v["mae"] for v in scores.values()])
    model = next(m for m in N.MODELS if m in scores and scores[m]["mae"] <= best * 1.05 + 1e-9)
    return model, N._predict(model, y, len(horizon)), scores[model]["rmse"]


@pytest.mark.parametrize("seed", range(8))
def test_auto_on_twelve_months_matches_the_original_algorithm(seed):
    rng = np.random.default_rng(seed)
    y = np.clip(np.cumsum(rng.normal(rng.uniform(-3, 8), 12, 12)) + 300, 5, None)
    s, hz = series(y, "2025-11-01"), pd.date_range("2026-11-01", periods=14, freq="MS")
    lf = N.forecast_line("x", s, hz)
    model, pred, rmse = _legacy_line(s, hz)
    assert lf.model == model
    capped = np.clip(np.minimum(np.maximum(pred, 0.5 * y[-3:].mean()), 2 * y[-3:].mean()), 0, None)
    assert lf.forecast.to_numpy() == pytest.approx(capped)


def test_default_bottom_up_is_the_table_the_page_always_showed(df, ny):
    for p in ("AWS", "GCP"):
        bu = B.run(df, ny, "account", p, "Auto", 12, 6)
        pd.testing.assert_frame_equal(bu.table, ny.tables[p])


# ------------------------------------------------------------------------------------------ forced models
def test_a_forced_model_is_used_everywhere_and_all_other_follows_it(df, ny):
    bu = B.run(df, ny, "account", "AWS", "Flat", 12, 2)
    assert {lf.model for lf in bu.lines} == {"Flat"}
    auto = B.run(df, ny, "account", "AWS", "Auto", 12, 2)
    assert [lf.model for lf in auto.lines if lf.name.startswith("All other")] == ["Damped trend"]


def test_a_model_that_lacks_history_falls_back_to_auto_and_says_so():
    s = series([0.0] * 14 + list(np.linspace(100, 130, 12)))                # only 12 active months
    lf = N.forecast_line("new", s, pd.date_range("2026-11-01", periods=14, freq="MS"), "Seasonal profile")
    assert lf.model in N.MODELS and "needs 18" in lf.note


def test_a_model_without_a_backtest_still_runs_but_is_flagged_low_confidence():
    s = seasonal(26, noise=2.0, slope=1.0)                                   # Holt-Winters fits at 24 but cannot be tested until 28
    lf = N.forecast_line("x", s, pd.date_range("2026-11-01", periods=14, freq="MS"), "Holt-Winters (seasonal)")
    assert lf.model == "Holt-Winters (seasonal)" and "backtest" in lf.note and lf.confidence == "Low"
    assert (lf.hi >= lf.forecast).all() and (lf.lo <= lf.forecast).all()


# ------------------------------------------------------------------------------------------ grouping
@pytest.mark.parametrize("dim", ["account", "project_code", "owner", "family", "environment", "provider_env", "service_name", "status", "service", "cost_center"])
def test_every_grouping_adds_up_to_the_provider_total(df, ny, dim):
    for scope in B.SCOPES:
        bu = B.run(df, ny, dim, scope, "Auto", 12, 2)
        window = df[(~df.is_partial) & (df.category == "Cloud Services") & df.provider.isin(B.providers_of(scope)) & df.date.isin(bu.index)]
        assert sum(lf.actual.sum() for lf in bu.lines) == pytest.approx(window.cost_net.sum() + bu.added_back)        # nothing dropped, nothing counted twice
        assert (bu.table[f"FY{bu.fy} forecast"] >= 0).all()


def test_spend_without_a_value_is_its_own_line_even_when_small(df, ny):
    bu = B.run(df, ny, "project_code", "AWS + GCP", "Auto", 12, 1)          # top_n=1: the unset group would normally fall into "All other"
    assert "(no project code)" in bu.table.index and bu.not_set == "(no project code)" and 0 < bu.not_set_share < 0.1
    assert any(i.startswith("All other") for i in bu.table.index)


def test_account_grouping_names_the_cloud_only_when_both_are_shown(df, ny):
    assert all("(AWS)" not in i for i in B.run(df, ny, "account", "AWS", "Auto", 12, 6).table.index)
    both = B.run(df, ny, "account", "AWS + GCP", "Auto", 12, 6)
    assert any(i.endswith("(AWS)") for i in both.table.index) and any(i.endswith("(GCP)") for i in both.table.index)


def test_project_code_groups_span_both_clouds(df, ny):
    bu = B.run(df, ny, "project_code", "AWS + GCP", "Auto", 12, 6)
    core = df[(df.project_code == "Core") & (df.category == "Cloud Services") & ~df.is_partial & df.date.isin(bu.index)]
    assert set(core.provider) == {"AWS", "GCP"}
    assert bu.table.loc["Core", "Accounts"] == core.account.nunique()


def test_savings_plan_gap_goes_to_the_account_that_pays_it(df, ny):
    bu = B.run(df, ny, "account", "AWS", "Flat", 12, 6)
    steady = next(lf for lf in bu.lines if lf.name == "Acct-Steady")
    assert steady.actual.min() >= 55_000 - 1e-6                              # 50K compute + the 5K Savings Plan, gap months filled back in
    assert bu.added_back > 0


def test_reconcile_scales_every_group_to_the_topdown_total(df, ny):
    raw = B.run(df, ny, "environment", "AWS + GCP", "Auto", 12, 6)
    rec = B.run(df, ny, "environment", "AWS + GCP", "Auto", 12, 6, reconcile=True)
    assert rec.total == pytest.approx(rec.topdown) and rec.scale == pytest.approx(rec.topdown / raw.total)
    assert (rec.table["Share"] == raw.table["Share"]).all()                  # history unchanged, only the forecast is scaled
    assert sum(lf.forecast.loc[ny.months].sum() for lf in rec.lines) == pytest.approx(rec.topdown)


def test_training_window_is_respected(df, ny):
    assert len(B.run(df, ny, "account", "AWS", "Auto", 18, 6).index) == 18
    assert len(B.run(df, ny, "account", "AWS", "Auto", 0, 6).index) == 27                          # all billed months (the partial one is excluded)
    short = B.run(df, ny, "account", "AWS", "Auto", 24, 6)
    assert len(short.index) == 24 and not short.notes
    only = B.run(df[df.date >= "2026-01-01"], ny, "account", "AWS", "Auto", 24, 6)
    assert len(only.index) == 10 and any("Only 10" in n for n in only.notes)


def test_longer_windows_make_seasonal_models_scorable(df, ny):
    assert B.run(df, ny, "account", "AWS", "Auto", 12, 4).board.drop(index="All lines (spend-weighted)").columns.isin(M.SEASONAL).sum() == 0
    allm = B.run(df, ny, "account", "AWS", "Auto", 0, 4)
    assert {"Seasonal naive × YoY", "Seasonal profile"} <= set(allm.board.columns)
    assert allm.board.attrs["lines"] == len(allm.lines)


def test_leaderboard_has_a_spend_weighted_row_and_names_the_model_used(df, ny):
    bu = B.run(df, ny, "account", "AWS", "Auto", 12, 4)
    assert "All lines (spend-weighted)" in bu.board.index
    assert list(bu.board.loc[[lf.name for lf in bu.lines], "Used"]) == [lf.model for lf in bu.lines]
    assert (bu.board.drop(columns="Used").stack() >= 0).all()


def test_drill_down_uses_the_same_model_and_window(df, ny):
    bu = B.run(df, ny, "project_code", "AWS + GCP", "Flat", 12, 6)
    svc = B.drill(df, ny, bu, "Core", "service")
    assert len(svc) >= 2 and set(svc.Model) == {"Flat"}
    acc = B.drill(df, ny, bu, "Core", "account")
    assert {"Acct-Steady (AWS)", "prj-ramp (GCP)"} <= set(acc.index)
    assert B.drill(df, ny, bu, "does-not-exist", "service").empty


def test_available_dims_follow_the_columns_present(df):
    assert B.available_dims(df) == list(B.DIMS)
    assert "project_code" not in B.available_dims(df.drop(columns="project_code"))


# ------------------------------------------------------------------------------------------ page
def test_page_offers_every_grouping_and_model_without_error():
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    root = Path(__file__).resolve().parent.parent
    at = AppTest.from_file(str(root / "views" / "next_year.py"), default_timeout=180).run()
    assert not at.exception, [e.value[:200] for e in at.exception]
    box = {s.label: s for s in at.selectbox}
    assert {"Group by", "Model", "Training window"} <= set(box)
    assert set(box["Model"].options) == {"Auto", *M.MODELS}
    for dim, model, win in (("environment", "Ensemble", "Last 24 months"), ("account", "Seasonal profile", "All months"), ("project_code", "Auto", "Last 18 months")):
        {s.label: s for s in at.selectbox}["Group by"].select(dim)
        {s.label: s for s in at.selectbox}["Model"].select(model)
        {s.label: s for s in at.selectbox}["Training window"].select(win)
        at.run()
        assert not at.exception, (dim, model, win, [e.value[:200] for e in at.exception])
    [c for c in at.checkbox if c.label == "Reconcile to top-down"][0].check().run()
    assert not at.exception


# ------------------------------------------------------------------------------------------ HTML report
def test_report_carries_the_grouping_model_and_window_chosen_on_the_page(df, ny):
    from finops import nextyear_report as R
    from finops import nextyear_summary as nys
    b, assumed = nys.default_budgets(ny.fy)
    budgets = {"AWS": b["AWS"], "GCP": b["GCP"]}
    bu = {sc: B.run(df, ny, "project_code", sc, "Ensemble", 18, 6, reconcile=True) for sc in B.SCOPES}
    html = R.build_report(ny, budgets, assumed, None, bu)
    assert "by project code (bottom-up)" in html and "Grouped by:" in html and "Ensemble" in html and "last 18 months" in html
    assert "reconciled to the top-down total" in html and "AWS + GCP together" in html
    plain = R.build_report(ny, budgets, assumed, None)                          # no selection: the original account / project report
    assert "by account and by project (bottom-up)" in plain and "Grouped by:" not in plain


def test_report_by_account_has_no_combined_page(df, ny):
    from finops import nextyear_report as R
    from finops import nextyear_summary as nys
    b, assumed = nys.default_budgets(ny.fy)
    bu = {sc: B.run(df, ny, "account", sc, "Auto", 12, 6) for sc in B.SCOPES}
    html = R.build_report(ny, {"AWS": b["AWS"], "GCP": b["GCP"]}, assumed, None, bu)
    assert "AWS accounts" in html and "AWS + GCP together" not in html


# ------------------------------------------------------------------------------------------ Month to Date chart
def test_month_to_date_chart_uses_three_distinct_colours_and_readable_hover(billing):
    from finops import asnow
    from finops.asnow_charts import budget_chart
    a = asnow.compute(billing)
    if a is None:
        pytest.skip("synthetic data has no in-progress month")
    fig = budget_chart(a)
    bars = [t for t in fig.data if t.type == "bar"]
    colors = [t.marker.color for t in bars]
    assert len(bars) == 3 and len(set(colors)) == 3 and all(isinstance(c, str) for c in colors)         # one colour per bar series, so the legend matches the bars
    assert "#94a3b8" not in colors                                                                          # the old grey
    assert fig.layout.hoverlabel.bgcolor == "#ffffff" and fig.layout.hovermode == "x unified"
