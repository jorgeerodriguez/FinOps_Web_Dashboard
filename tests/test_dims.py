"""Project codes / environments / cost centers: joining the owner-file columns, the tables, findings, pages and reports."""
from __future__ import annotations
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from finops import dims as D
from finops.data import load_all
from finops.owners import MISSING
from tests import synth

ROOT = Path(__file__).resolve().parent.parent

AWS = ("account_id,account_name,family,environment,service_name,owner,department,cost_center,project_code,treatment,finance_notes\n"
       ",Acct-Steady,f,Production,Svc A,Alice,,,Alpha,,Core platform\n"
       ",Acct-Shrunk,f,nonprod,Svc B,Alice,,,alpha ,,\n"                         # same code, different case and a trailing space
       ",Acct-Ended,f,prod,,Bob,,,PLACEHOLDER,,\n"                                 # a placeholder is 'not filled in'
       ",Acct-Spike,f,prod,,Bob,,,Shared Bucket,,Enterprise fees\n")
GCP = ("project,family,environment,owner,department,cost_center,project_code,treatment,finance_notes\n"
       "prj-ramp,f,Non Prod,Cara,,1000-0001,ALPHA,,Warehouse\n"
       "prj-small,f,,Cara,,TBD,,,\n")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FINOPS_CONFIG", str(synth.make_env(tmp_path)))
    (tmp_path / "data" / "owners_aws.csv").write_text(AWS)
    (tmp_path / "data" / "owners_gcp.csv").write_text(GCP)
    with open(tmp_path / "config.toml", "a") as f:
        f.write('\n[dimensions]\nshared_codes = ["shared bucket"]\n')
    return tmp_path


@pytest.fixture
def cloud(env):
    d = load_all()
    return d[d.category == "Cloud Services"]


# ------------------------------------------------------------------ joining the columns
def test_values_are_tidied_and_spelled_the_same_across_both_clouds(cloud):
    m = cloud.drop_duplicates("account").set_index("account")
    assert m.loc["Acct-Steady", "project_code"] == m.loc["Acct-Shrunk", "project_code"] == m.loc["prj-ramp", "project_code"]   # Alpha / alpha / ALPHA -> one spelling
    assert m.loc["Acct-Ended", "project_code"] == MISSING["project_code"] and m.loc["prj-small", "project_code"] == MISSING["project_code"]
    assert m.loc["Acct-Steady", "environment"] == "prod" and m.loc["Acct-Shrunk", "environment"] == "non-prod" and m.loc["prj-ramp", "environment"] == "non-prod"
    assert m.loc["prj-small", "environment"] == "unspecified" and m.loc["prj-small", "cost_center"] == MISSING["cost_center"]       # blank / TBD
    assert m.loc["prj-ramp", "cost_center"] == "1000-0001" and m.loc["Acct-Steady", "finance_notes"] == "Core platform"


def test_without_owner_files_every_column_exists_with_the_missing_value(tmp_path, monkeypatch):
    monkeypatch.setenv("FINOPS_CONFIG", str(synth.make_env(tmp_path)))
    d = load_all()
    for c, v in MISSING.items():
        assert (d[c] == v).all()


def test_aws_rows_match_by_account_id_when_the_name_differs(tmp_path, monkeypatch):
    from finops import owners
    monkeypatch.setenv("FINOPS_CONFIG", str(synth.make_env(tmp_path)))
    (tmp_path / "data" / "owners_aws.csv").write_text("account_id,account_name,family,environment,owner,project_code\n000000000123,Old Name,f,prod,A,Zed\n")
    b = synth.make_billing(3)
    b["account"] = b["account"].where(b["account"] != "Acct-Steady", "000000000123")             # billing only knew the id
    out = owners.attach_dims(b.copy())
    assert (out[out.account == "000000000123"]["project_code"] == "Zed").all()


# ------------------------------------------------------------------ tables
def test_by_dim_windows_share_notes_and_shared_flag(cloud):
    t = D.by_dim(cloud, "project_code", ["Shared Bucket"])
    cur, prev, _ = D.windows(cloud)
    assert t["cur"].sum() == pytest.approx(cur.cost_net.sum()) and t["prev"].sum() == pytest.approx(prev.cost_net.sum())
    assert t["share"].sum() == pytest.approx(1.0)
    assert bool(t.loc["Shared Bucket", "shared"]) and not bool(t.loc["Alpha", "shared"])
    assert t.loc["Alpha", "notes"] == "Core platform · Warehouse" and t.loc["Alpha", "providers"] == "AWS + GCP"
    disp = D.display(t, "project_code")
    assert disp["Project code"].str.startswith("★ ").sum() == 1


def test_change_text():
    assert D.chg(float("nan")) == "–" and D.chg(0.12) == "+12%" and D.chg(-0.3) == "-30%" and D.chg(56.0) == "57×"


def test_coverage_and_readiness(cloud):
    c = D.coverage(cloud, "cost_center")
    assert 0 < c["share"] < 1 and c["missing"] > 0
    r = D.readiness(cloud)
    assert set(r.Field) == {"Project code", "Environment", "Cost center"} and set(r.Provider) == {"AWS", "GCP"}
    assert D.coverage(cloud, "project_code")["share"] < 1                                       # Acct-Ended + prj-small have none


def test_prod_vs_nonprod_by_code(cloud):
    e = D.env_by_code(cloud)
    a = e.loc["Alpha"]
    assert a["prod"] > 0 and a["non-prod"] > 0 and a["nonprod_share"] == pytest.approx(a["non-prod"] / (a["prod"] + a["non-prod"]))
    assert D.env_display(e).shape[0] == len(e)


def test_projects_on_both_clouds(cloud):
    cc = D.cross_cloud(cloud, min_each=1_000)
    assert list(cc.index) == ["Alpha"] and cc.loc["Alpha", "AWS"] > 0 and cc.loc["Alpha", "GCP"] > 0
    assert D.cross_cloud(cloud[cloud.provider == "AWS"]).empty                                   # one cloud only: nothing, and no crash
    assert "GCP share" in D.cross_display(cc)


def test_findings_name_the_shared_bucket_and_the_gaps(cloud):
    text = " ".join(D.narrative(cloud, ["Shared Bucket"]))
    assert "Shared buckets" in text and "Shared Bucket" in text and "Environments" in text and "On both clouds" in text
    assert "no project code" not in text                                                       # under 2% of spend: not worth a line
    gap = " ".join(D.narrative(cloud.assign(project_code=MISSING["project_code"]), []))
    assert "no project code" in gap and "Biggest projects" not in gap
    assert len(D.kpi_items(cloud, ["Shared Bucket"])) == 5


def test_every_chart_builds_for_both_clouds_and_for_a_single_cloud(cloud):
    for d in (cloud, cloud[cloud.provider == "AWS"], cloud[cloud.provider == "GCP"]):
        t = D.by_dim(d, "project_code", ["Shared Bucket"])
        for f in (D.fig_stack(d, "project_code", 5), D.fig_change(t, "project_code"), D.fig_env(d), D.fig_nonprod_share(d), D.fig_treemap(d, ["Shared Bucket"]),
                  D.fig_cross(D.cross_cloud(d, 1_000))):
            assert f is not None


# ------------------------------------------------------------------ pages and reports
def page(name):
    return AppTest.from_file(str(ROOT / "views" / f"{name}.py"), default_timeout=180).run()


@pytest.mark.parametrize("prov", ["aws", "gcp"])
def test_provider_page_shows_projects_and_environments_and_the_new_views(env, prov):
    at = page(prov)
    assert not at.exception, [e.value[:300] for e in at.exception]
    assert any(s.value == "Projects & environments" for s in at.subheader)
    assert any("Shared buckets" in m.value for m in at.markdown) == (prov == "aws")            # the shared code only exists on the AWS side of the test data
    third = "Cost center" if prov == "gcp" else "Service name"
    for view in ("Project code", "Environment", third):
        at.sidebar.radio[0].set_value(view).run()
        assert not at.exception, (view, [e.value[:300] for e in at.exception])


def test_executive_page_has_the_project_section(env):
    at = page("executive")
    assert not at.exception, [e.value[:300] for e in at.exception]
    assert any(s.value.startswith("Spend by project") for s in at.subheader)
    assert any("Finance readiness" in m.value for m in at.markdown)


def test_projects_report_and_the_executive_report_include_the_project_pages(env):
    from finops import dims_report, exec_report
    h = dims_report.build_report()
    assert "Projects &amp; Environments" in h and "Finance readiness" in h and "Shared / enterprise buckets" in h
    assert dims_report.save_report(h).name.startswith("FinOps_Projects_Environments_")
    assert "Spend by project" in exec_report.build_report()


def test_owner_editor_filters_rows_that_still_need_something(env):
    at = page("data_health")
    assert not at.exception, [e.value[:300] for e in at.exception]
    r = at.radio(key="own_AWS_show")
    assert r.options[0].startswith("Without an owner") and any(o.startswith("Without a project code") for o in r.options)
    for o in r.options:
        r.set_value(o).run()
        assert not at.exception, (o, [e.value[:300] for e in at.exception])
