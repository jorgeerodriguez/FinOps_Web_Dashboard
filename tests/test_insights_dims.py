"""Insights & Savings with project codes: the project-code lens, ledger annotations, scorecard by owner / project / cost center, opportunities, review pack."""
from __future__ import annotations
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from finops import dims as D
from finops import history as H
from finops import insights as I
from finops import opportunities as O
from finops import savings as S
from finops.data import load_all
from tests.test_dims import env  # noqa: F401  (fixture: owner files with project codes, a shared bucket)

ROOT = Path(__file__).resolve().parent.parent
NO_RULES = pd.DataFrame(columns=["provider", "account", "service", "months", "action", "note"])


@pytest.fixture
def cloud(env):  # noqa: F811
    d = load_all()
    return d[d.category == "Cloud Services"]


def migration_frame() -> pd.DataFrame:
    """Project 'Alpha' moves $50K/month from account A to account B at month 14: a migration, not a saving. Project 'Beta' really shrinks."""
    idx = pd.date_range(end="2026-10-01", periods=27, freq="MS")
    rows = []
    for i, d in enumerate(idx):
        a, b = (50_000.0, 0.0) if i < 14 else (0.0, 50_000.0)
        beta = 40_000.0 if i < 14 else 10_000.0
        for acct, code, v in (("A", "Alpha", a), ("B", "Alpha", b), ("C", "Beta", beta)):
            rows.append(dict(date=d, provider="AWS", account=acct, service="EC2", category="Cloud Services", env="x", cost_gross=v, credits=0.0, cost_net=v,
                             owner="o", family=acct, is_partial=False, environment="prod", project_code=code, cost_center="cc", service_name="", finance_notes=""))
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ the project-code lens
def test_a_migration_between_accounts_is_not_a_saving_at_project_level():
    df = migration_frame()
    by_acct = S.scan(S.adjusted_series(df, "account", NO_RULES), 5000.0)
    by_code = S.scan(S.adjusted_series(df, "project_code", NO_RULES), 5000.0)
    assert {"A", "C"} <= set(by_acct.ledger.name)                                              # account A "ended": looks like a shutdown
    assert "Alpha" not in set(by_code.ledger.name) and "Beta" in set(by_code.ledger.name)       # at project level only the real reduction remains
    assert set(by_code.ledger.provider) == {S.ALL}
    assert by_code.ledger.set_index("name").loc["Beta", "lower"] == pytest.approx(30_000.0)


def test_project_code_series_and_owner_map_span_both_clouds(cloud):
    s = S.series(cloud, "project_code")
    assert {p for p, _ in s.columns} == {S.ALL} and "Alpha" in {n for _, n in s.columns}
    assert s.sum().sum() == pytest.approx(cloud.cost_net.sum())
    om = S.owners_map(cloud, "project_code")
    assert (S.ALL, "Alpha") in om and om[(S.ALL, "Alpha")] in ("Alice", "Cara", "Bob", "")


def test_billing_gaps_are_added_back_on_the_project_code_lens_too(cloud):
    plain, adj = S.series(cloud, "project_code"), S.adjusted_series(cloud, "project_code", NO_RULES)
    assert adj.sum().sum() >= plain.sum().sum()                                                 # Savings Plans fee gaps filled, never removed
    assert (adj >= plain - 1e-9).all().all()


def test_ledger_rows_are_annotated_with_project_code_environment_and_cost_center(cloud):
    sc = S.annotate(S.scan(S.adjusted_series(cloud, "account"), 5000.0), cloud, "account")
    assert {"project_code", "environment", "cost_center"} <= set(sc.ledger.columns)
    row = sc.ledger.set_index("name").loc["Acct-Shrunk"]
    assert row.project_code == "Alpha" and row.environment == "non-prod"
    disp = S.ledger_display(sc.ledger)
    assert {"Project code", "Environment", "Cost center"} <= set(disp.columns)
    landed = S.landed(sc.ledger, "project_code")
    assert landed.loc["Alpha", "realized"] > 0 and S.fig_landed(landed, "x") is not None
    assert S.landed(sc.ledger, "nope").empty


# ------------------------------------------------------------------ scorecard
@pytest.mark.parametrize("by", ["owner", "project_code", "cost_center"])
def test_scorecard_groups_by_owner_project_code_or_cost_center(cloud, by):
    h = H.compute(cloud)
    acct = S.annotate(S.scan(S.adjusted_series(cloud, "account"), 5000.0, S.owners_map(cloud, "account")), cloud, "account")
    t = S.owner_scorecard(cloud, h, acct, by)
    assert t.latest.sum() == pytest.approx(cloud[cloud.date.isin(h.cur)].cost_net.sum()) and t.index.name == by
    assert S.scorecard_display(t).columns[0] == S.BY_LABEL[by]
    first = t.index[0]
    d = S.owner_detail(cloud, h, first, acct, by=by)
    assert d["latest"] == pytest.approx(t.loc[first, "latest"]) and isinstance(d["notes"], list)
    total = acct.ledger[acct.ledger.cls == "Reduced"]["lower"].sum()
    if by == "owner":
        assert t.saving_mo.sum() <= total + 1e-6                                                # lines with no owner sit under "(unassigned)" only if named so
    else:
        assert t.saving_mo.sum() == pytest.approx(total) and total > 0                          # every reduced line is credited to exactly one group


def test_project_notes_reach_the_review(cloud):
    h = H.compute(cloud)
    acct = S.annotate(S.scan(S.adjusted_series(cloud, "account"), 5000.0), cloud, "account")
    assert "Core platform" in S.owner_detail(cloud, h, "Alpha", acct, by="project_code")["notes"]


# ------------------------------------------------------------------ detectors keep working on the project-code lens, with notes
def test_detectors_group_by_project_code_and_carry_notes(cloud):
    month = cloud.date.max()
    for fn in (I.spikes, I.new_spend, I.sustained_growth):
        t = fn(cloud, "project_code", month)
        assert (t.provider == S.ALL).all() if len(t) else True
    t = I.add_notes(pd.DataFrame({"provider": [S.ALL], "project_code": ["Alpha"]}), cloud, "project_code")
    assert t["What it is"].iloc[0] == "Core platform · Warehouse"
    assert "What it is" not in I.add_notes(pd.DataFrame({"service": ["x"]}), cloud, "service")


# ------------------------------------------------------------------ opportunities
def test_nonprod_heavy_projects_and_steady_lines(cloud):
    e = D.env_by_code(cloud)
    npv = O.nonprod_heavy(cloud, 1_000)
    assert set(npv.index) == set(e[(e["non-prod"] >= 1_000) & (e["non-prod"] >= e["prod"])].index) - {"(no project code)"}
    assert O.nonprod_display(npv).shape[0] == len(npv)
    st = O.steady_lines(cloud)
    assert ("AWS", "Acct-Steady", "Amazon Elastic Compute Cloud - Compute") in set(zip(st.provider, st.account, st.service))
    assert not st.service.str.contains("Savings Plans|Support").any()                          # not commitment candidates
    assert "Compute Savings Plan" in st[st.account == "Acct-Steady"].hint.iloc[0]
    assert O.steady_display(st).shape[0] == len(st)
    assert O.steady_lines(cloud, min_monthly=10_000_000).empty


def test_credits_watch_flags_shrinking_credits_and_handles_none(cloud):
    assert O.credits_watch(cloud).empty                                                          # the synthetic data has no credits
    idx = pd.date_range(end="2026-10-01", periods=14, freq="MS")
    cr = [-15_000.0] * 10 + [-6_000.0] * 4
    df = pd.DataFrame({"date": idx, "provider": "GCP", "account": "p", "service": "s", "category": "Cloud Services", "cost_gross": 400_000.0, "credits": cr, "cost_net": 400_000.0 + np.array(cr)})
    t = O.credits_watch(df)
    lines = O.credits_lines(t)
    assert len(lines) == 1 and "shrinking" in lines[0] and O.fig_credits(t) is not None


# ------------------------------------------------------------------ page and review pack
def page():
    return AppTest.from_file(str(ROOT / "views" / "insights.py"), default_timeout=240).run()


def test_insights_page_lenses_grouping_opportunities_and_gaps(env):  # noqa: F811
    at = page()
    assert not at.exception, [e.value[:300] for e in at.exception]
    assert [t.label for t in at.tabs][:3] == ["Savings ledger", "Owners & projects", "Opportunities"]
    assert any(m.label == "Steady always-on lines" for m in at.metric)
    assert any(s.value == "Where the savings landed" for s in at.subheader) and any(s.value.startswith("Finance gaps") for s in at.subheader)
    for lens in ("Project code", "Service", "Account / Project"):
        at.sidebar.radio[0].set_value(lens).run()
        assert not at.exception, (lens, [e.value[:300] for e in at.exception])
    for by in ("Project code", "Cost center", "Owner"):
        at.radio(key="sc_by").set_value(by).run()
        assert not at.exception, (by, [e.value[:300] for e in at.exception])


def test_review_pack_for_an_owner_a_project_and_a_cost_center(env):  # noqa: F811
    from finops.review_report import build_review, save_review
    for by, who in (("owner", "Alice"), ("project_code", "Alpha"), ("cost_center", "1000-0001")):
        html = build_review(by, who)
        assert f"FinOps review — {who}" in html and "Open gaps" in html and "What changed" in html
    assert save_review(html, "cost_center", "1000-0001").name.startswith("FinOps_Review_cost_center_1000_0001_")
    assert "Core platform" in build_review("project_code", "Alpha")


def test_review_button_builds_the_pack(env):  # noqa: F811
    at = page()
    [b for b in at.button if b.label == "Build review pack"][0].click().run()
    assert not at.exception, [e.value[:300] for e in at.exception]
    name = at.session_state["review_pack"][1]
    assert name.startswith("FinOps_Review_owner_") and "FinOps review" in at.session_state["review_pack"][0]
