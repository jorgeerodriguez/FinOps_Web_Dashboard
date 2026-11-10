import numpy as np
import pandas as pd
import pytest

from finops import forecast as F
from finops import nextyear as N


def series(vals, start="2024-09-01"):
    return pd.Series(vals, index=pd.date_range(start, periods=len(vals), freq="MS"), dtype=float)


def test_combine_marks_a_month_actual_only_if_every_provider_is_actual():
    """Regression: a scalar `and` once tagged every month 'forecast'."""
    mk = lambda kinds: pd.DataFrame({"date": pd.date_range("2026-01-01", periods=3, freq="MS"), "value": 1.0, "low": 1.0, "high": 1.0, "kind": kinds})
    c = F.combine([mk(["actual", "actual", "forecast"]), mk(["actual", "forecast", "forecast"])])
    assert list(c.kind) == ["actual", "forecast", "forecast"]
    assert (c.value == 2.0).all()


def test_year_forecast_keeps_actuals_and_orders_the_band():
    s = series(np.linspace(100, 200, 14))                    # Sep 2024 .. Oct 2025
    f = F.year_forecast(s, 2025)
    assert len(f) == 12
    act = f[f.kind == "actual"]
    assert list(act.value) == list(s[s.index.year == 2025])
    fc = f[f.kind == "forecast"]
    assert len(fc) == 2 and (fc.low <= fc.value).all() and (fc.value <= fc.high).all() and (fc.value >= 0).all()


def test_projection_is_never_negative():
    p = F.project(series([100, 80, 60, 40, 20, 5, 1, 0, 0, 0, 0, 0, 0, 0]), pd.date_range("2025-11-01", periods=6, freq="MS"), "runrate")
    assert (p >= 0).all()


def test_short_history_falls_back_to_runrate():
    assert F.pick_method(series([100.0] * 8))[0] == "runrate"


def test_theil_sen_is_robust_and_exact_on_lines():
    assert N.theil_sen([1, 2, 3, 4, 5]) == pytest.approx(1.0)
    assert N.theil_sen([7, 7, 7, 7]) == 0.0
    assert abs(N.theil_sen([10, 11, 12, 500, 14, 15, 16]) - 1.0) < 1.0      # one wild month barely moves it


def test_topdown_scenarios_are_ordered_and_flat_stays_flat():
    hz = pd.date_range("2026-11-01", periods=14, freq="MS")
    rising = series(np.linspace(100, 220, 12), "2025-11-01")
    out, anchor, slope, _ = N.topdown(rising, pd.Series(0.0, index=rising.index), hz)
    assert slope > 0 and anchor == pytest.approx(rising.tail(3).mean())
    assert (out.Low <= out.Base + 1e-9).all() and (out.Base <= out.High + 1e-9).all()
    flat = series([100.0] * 12, "2025-11-01")
    out, anchor, slope, _ = N.topdown(flat, pd.Series(0.0, index=flat.index), hz)
    assert slope == 0 and (out == 100.0).all().all()


def test_topdown_trend_since_ignores_an_early_ramp():
    s = series([10, 20, 40, 80, 100, 100, 100, 100, 100, 100, 100, 100], "2025-11-01")
    hz = pd.date_range("2026-11-01", periods=2, freq="MS")
    z = pd.Series(0.0, index=s.index)
    _, _, slope_all, _ = N.topdown(s, z, hz)
    _, _, slope_since, _ = N.topdown(s, z, hz, trend_from=pd.Timestamp("2026-03-01"))
    assert slope_since == 0 and slope_all > 0


def test_next_year_build_is_consistent(billing):
    b = billing[~billing.is_partial]
    ny = N.build(b, top_n=3, apply_adjustments=False)
    assert ny.fy == 2027 and len(ny.months) == 12 and ny.basis[-1] == b.date.max()
    for p in ("AWS", "GCP"):
        f = ny.svc[p]
        assert (f.Low <= f.Base + 1e-6).all() and (f.Base <= f.High + 1e-6).all()
        assert len(ny.hist[p]) == 27                       # full history kept as context
        assert len(ny.tables[p]) >= 1
    assert ny.mkt.loc[ny.months].sum().sum() > 0           # marketplace calendar produced charges
