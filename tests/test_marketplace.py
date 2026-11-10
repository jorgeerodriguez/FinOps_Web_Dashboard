import numpy as np
import pandas as pd
import pytest

from finops import marketplace as M

BASIS = pd.date_range("2025-11-01", periods=12, freq="MS")
HORIZON = pd.date_range("2026-11-01", periods=14, freq="MS")


def sched(values):
    return M.contract_schedule(pd.Series(values, index=BASIS, dtype=float), HORIZON)


def test_monthly_contract_repeats_the_recent_average():
    cad, amt, s = sched([20_000.0] * 12)
    assert cad == "Monthly" and amt == 20_000 and (s == 20_000).all()


def test_quarterly_contract_repeats_every_three_months():
    v = [0.0] * 12
    for i in (1, 4, 7, 10):
        v[i] = 30_000.0
    cad, amt, s = sched(v)
    assert cad == "Quarterly" and amt == 30_000
    charged = s[s > 0].index
    assert len(charged) >= 4 and all((b.year * 12 + b.month) - (a.year * 12 + a.month) == 3 for a, b in zip(charged, charged[1:]))


def test_single_charge_is_an_annual_renewal_same_month_next_year():
    v = [0.0] * 12
    v[4] = 60_000.0                                           # Mar 2026
    cad, amt, s = sched(v)
    assert cad == "Annual" and list(s[s > 0].index) == [pd.Timestamp("2027-03-01")]


def test_small_trueups_are_ignored_when_reading_the_cadence():
    v = [0.0] * 12
    for i in (1, 4, 7):
        v[i] = 30_000.0
    v[2] = 500.0                                              # a small true-up between quarters
    assert sched(v)[0] == "Quarterly"


def test_five_or_more_charges_in_a_year_are_read_as_monthly_even_if_some_are_tiny():
    """Documents current behaviour (a known edge case): the 5-charge 'monthly' rule runs BEFORE the small-charge filter,
    so a quarterly contract plus one true-up is scheduled as a flat monthly average. Annual dollars are about the same; timing differs."""
    v = [0.0] * 12
    for i in (1, 4, 7, 10):
        v[i] = 30_000.0
    v[2] = 500.0
    assert sched(v)[0] == "Monthly"


def test_nothing_billed_returns_none():
    assert sched([0.0] * 12) is None


def test_contract_folding_and_vendor_labels():
    assert M.contract_key("Claude 3.5 Sonnet (Amazon Bedrock Edition)") == M.contract_key("Claude Platform")
    assert M.vendor_label("Datadog Pro - Annual") == "Datadog"
    assert M.vendor_label("x" * 60).endswith("…")


def test_build_compares_with_the_prior_12_months(billing):
    mk = billing[(billing.category == "Marketplace") & ~billing.is_partial]
    ct, m, last = M.build(mk, pd.Timestamp("2027-12-01"))
    d = ct.set_index("contract")
    assert d.loc["Datadog Pro", "billed_12mo"] == 240_000 and d.loc["Datadog Pro", "billed_prev12"] == 240_000
    assert d.loc["Datadog Pro", "cadence"] == "Monthly"
    assert d.loc["Vendor Q (reseller)", "cadence"] == "Quarterly"
    assert m.index[-1] == pd.Timestamp("2027-12-01")


def test_prior_comparison_is_nan_without_enough_history(billing):
    mk = billing[(billing.category == "Marketplace") & ~billing.is_partial]
    ct, _, _ = M.build(mk[mk.date >= "2025-11-01"], pd.Timestamp("2027-12-01"))
    assert ct.billed_prev12.isna().all()
