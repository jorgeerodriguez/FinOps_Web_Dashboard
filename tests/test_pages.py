"""Smoke tests: every page and every HTML report must run, on synthetic data and on a brand-new empty install."""
from __future__ import annotations
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
PAGES = sorted(p.stem for p in (ROOT / "views").glob("*.py"))
pytestmark = pytest.mark.slow


def run(page: str) -> AppTest:
    return AppTest.from_file(str(ROOT / "views" / f"{page}.py"), default_timeout=120).run()


def test_every_page_is_in_the_navigation():
    nav = (ROOT / "app.py").read_text()
    missing = [p for p in PAGES if f"views/{p}.py" not in nav]
    assert not missing, f"views not reachable from app.py: {missing}"


@pytest.mark.parametrize("page", PAGES)
def test_page_runs_with_data(page):
    at = run(page)
    assert not at.exception, [e.value[:300] for e in at.exception]


@pytest.mark.parametrize("page", PAGES)
def test_page_survives_an_empty_install(page, empty_env):
    at = run(page)
    assert not at.exception, [e.value[:300] for e in at.exception]


def test_history_page_content():
    at = run("history")
    assert not at.exception
    assert any("Last 12 months" in m.label for m in at.metric)
    assert any("Executive summary" in s.value for s in at.subheader)


def test_insights_lenses_and_owner_review():
    at = run("insights")
    assert not at.exception
    look = lambda: [r for r in at.sidebar.radio if r.label == "Look at"][0]
    look().set_value("Service").run()
    assert not at.exception, [e.value[:300] for e in at.exception]
    assert any("service" in m.value.lower() for m in at.markdown)
    look().set_value("Account / Project").run()
    owner = [s for s in at.selectbox if s.label == "Owner"][0]
    for who in owner.options:
        owner.set_value(who).run()
        assert not at.exception, who
        owner = [s for s in at.selectbox if s.label == "Owner"][0]


def test_reports_build():
    from finops import exec_report, history_report, marketplace_report, nextyear, nextyear_report, nextyear_summary
    from finops import history as H
    from finops.ui import force_view, get_data
    for html in (exec_report.build_report(), history_report.build_report(), marketplace_report.build_report()):
        assert html.startswith("<!doctype html>") and "</html>" in html and len(html) > 100_000
    with force_view("Cloud Services (budget basis)"):
        df = get_data(apply_view=False)
        ny = nextyear.build(df, 3, True)
        b, assumed = nextyear_summary.default_budgets(ny.fy)
        html = nextyear_report.build_report(ny, {"AWS": b["AWS"], "GCP": b["GCP"]}, assumed, H.lookback(df))
        assert "How did this method do last year?" in html
