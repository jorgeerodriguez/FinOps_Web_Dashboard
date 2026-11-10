import numpy as np
import pandas as pd
import pytest

from finops import asnow, schema, store
from finops.ui import money, pct
from tests import synth


@pytest.mark.parametrize("v,s", [(0, "$0"), (950, "$950"), (12_345, "$12K"), (999_499, "$999K"), (999_500, "$1.00M"), (2_500_000, "$2.50M"),
                                 (-1_500, "-$1,500"), (-2_500_000, "-$2.50M"), (float("nan"), "–")])
def test_money(v, s):
    assert money(v) == s


def test_pct():
    assert pct(0.0123) == "+1.2%" and pct(-0.05) == "-5.0%" and pct(float("nan")) == "–"


def test_project_lines_cases():
    mtd = pd.Series({"lump": 900.0, "notyet": 0.0, "steady": 500.0})
    last = pd.Series({"lump": 1_000.0, "notyet": 800.0, "steady": 1_000.0})
    out = asnow._project_lines(mtd, last, frac=0.5)
    assert out.loc["lump", "method"] == "Already billed" and out.loc["lump", "projected"] == 900
    assert out.loc["notyet", "method"].startswith("Not posted") and out.loc["notyet", "projected"] == 800
    assert out.loc["steady", "method"] == "Run-rate" and out.loc["steady", "projected"] == pytest.approx(1_000)


def test_ahead_of_pace_is_capped():
    # late enough in the month that a big amount is not a "lump" (frac >= LUMP_BEFORE), and the run-rate is far above last month
    out = asnow._project_lines(pd.Series({"x": 2_800.0}), pd.Series({"x": 2_000.0}), frac=0.7)
    assert out.loc["x", "method"].startswith("Ahead")
    assert out.loc["x", "projected"] == pytest.approx(asnow.AHEAD_CAP * 2_000)


def test_lump_rule_does_not_apply_late_in_the_month():
    out = asnow._project_lines(pd.Series({"x": 900.0}), pd.Series({"x": 1_000.0}), frac=0.9)
    assert out.loc["x", "method"] != "Already billed"


def test_finalize_fills_defaults_and_month_start():
    d = schema.finalize(pd.DataFrame({"date": ["2026-03-17"], "provider": ["AWS"], "account": ["a"], "service": ["s"], "cost_net": [5.0]}))
    assert list(d.columns) == schema.COLUMNS
    assert d.loc[0, "date"] == pd.Timestamp("2026-03-01") and d.loc[0, "category"] == "Cloud Services" and d.loc[0, "owner"] == "(unassigned)"


def test_upsert_replaces_only_that_providers_months(tmp_path, monkeypatch):
    monkeypatch.setenv("FINOPS_CONFIG", str(synth.make_env(tmp_path, with_data=False)))
    base = synth.make_billing(6, partial=False)
    store.upsert(base[base.provider == "AWS"].copy(), "AWS")
    store.upsert(base[base.provider == "GCP"].copy(), "GCP")
    before = store.read()
    redo = base[(base.provider == "AWS") & (base.date == base.date.max())].copy()
    redo["cost_net"] = redo["cost_gross"] = 1.0
    store.upsert(redo, "AWS")
    after = store.read()
    assert len(store.read_log()) == 3
    assert after[(after.provider == "GCP")].cost_net.sum() == before[(before.provider == "GCP")].cost_net.sum()       # untouched
    assert after[(after.provider == "AWS") & (after.date == base.date.max())].cost_net.sum() == pytest.approx(len(redo))
    old = before[(before.provider == "AWS") & (before.date < base.date.max())].cost_net.sum()
    assert after[(after.provider == "AWS") & (after.date < base.date.max())].cost_net.sum() == pytest.approx(old)     # earlier months kept
