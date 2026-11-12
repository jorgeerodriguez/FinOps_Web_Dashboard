"""Cloud tags: shaping what the ingest fetched, reading it back, comparing with the owner file, and the Data Health page."""
from __future__ import annotations
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from finops import editors as E
from finops import tags as T

ROOT = Path(__file__).resolve().parent.parent
CFG = {**T.DEFAULTS, "months": 3}


def acct():
    return pd.DataFrame({"account_id": ["111111111111", "222222222222", "333333333333"], "account": ["Alpha-Prod", "Beta-Dev", "Gamma"], "total": [1000.0, 500.0, 200.0]})


def rows(*r):
    return pd.DataFrame(list(r), columns=["key", "account_id", "account", "value", "cost"])


# ------------------------------------------------------------------ choosing keys
def test_aws_only_active_user_defined_keys_are_pulled_and_inactive_ones_are_explained():
    known = [{"TagKey": "Team", "Status": "Active", "Type": "UserDefined"}, {"TagKey": "cost-center", "Status": "Inactive", "Type": "UserDefined"},
             {"TagKey": "aws:createdBy", "Status": "Active", "Type": "AWSGenerated"}, {"TagKey": "Name", "Status": "Active", "Type": "UserDefined"}]
    pull, why = T.aws_keys(known, CFG)
    assert pull == ["Team"]
    assert "activate" in why["cost-center"] and "excluded" in why["Name"]


def test_aws_listed_keys_override_auto_and_unknown_listed_keys_are_reported():
    known = [{"TagKey": "Team", "Status": "Active", "Type": "UserDefined"}, {"TagKey": "Project", "Status": "Active", "Type": "UserDefined"}]
    pull, why = T.aws_keys(known, {**CFG, "aws": ["Project", "Nope"]})
    assert pull == ["Project"] and "does not know" in why["Nope"]


def gcp_raw():
    r = [("p1", "Alpha-Prod", "cost-center", "1234", "project", 900.0), ("p1", "Alpha-Prod", "cost-center", "OTHER", "resource", 50.0),
         ("p1", "Alpha-Prod", "goog-managed-by", "x", "resource", 300.0), ("p2", "Beta-Dev", "environment", "dev", "resource", 400.0)]
    return pd.DataFrame(r, columns=["account_id", "account", "key", "value", "src", "cost"])


def test_gcp_project_label_beats_a_resource_label_and_goog_keys_are_left_out():
    out, status, why = T.gcp_rows(gcp_raw(), CFG)
    assert set(out["key"]) == {"cost-center", "environment"}
    assert out[out.key == "cost-center"]["value"].tolist() == ["1234"]               # the resource-level OTHER lost to the project label
    assert "goog-managed-by" in why and status["cost-center"] == "label on the project"


def test_gcp_listed_keys_and_max_keys():
    out, _, why = T.gcp_rows(gcp_raw(), {**CFG, "gcp": ["environment", "missing"]})
    assert set(out["key"]) == {"environment"} and "missing" in why
    out, _, why = T.gcp_rows(gcp_raw(), {**CFG, "max_keys": 1})
    assert len(set(out["key"])) == 1 and any("biggest" in v for v in why.values())


# ------------------------------------------------------------------ build / reading back
def test_build_drops_identifier_like_keys_and_records_why():
    many = rows(*[("Id", "111111111111", "Alpha-Prod", f"v{i}", 1.0) for i in range(5)], ("Team", "111111111111", "Alpha-Prod", "Ops", 10.0), ("Team", "222222222222", "Beta-Dev", "", 5.0))
    a, v, c = T.build("AWS", acct(), many, {"Id": "Active", "Team": "Active", "Old": "Inactive"}, "2026-08..2026-10", {**CFG, "max_values": 3}, {"Old": "inactive in AWS"})
    assert set(v["key"]) == {"Team"} and len(v) == 1                                      # the empty value is not a tag
    cat = c.set_index("key")
    assert not cat.loc["Id", "pulled"] and "identifier" in cat.loc["Id", "note"] and cat.loc["Old", "note"] == "inactive in AWS" and cat.loc["Team", "pulled"]


def table(*r):
    a = acct()
    v = pd.DataFrame(list(r), columns=["key", "account_id", "account", "value", "cost"])
    return T.build("AWS", a, v, {k: "Active" for k in set(v["key"])}, "w", CFG)


def test_dominant_value_count_and_coverage():
    a, v, _ = table(("cost-center", "111111111111", "Alpha-Prod", "1234", 600.0), ("cost-center", "111111111111", "Alpha-Prod", "9999", 100.0))
    d = T.dominant(v, a).iloc[0]
    assert d.value == "1234" and d.n_values == 2 and d.coverage == pytest.approx(0.7)
    assert T.tag_text(d.value, d.n_values, d.coverage) == "1234 +1 · 70%"
    assert T.tag_text("a", 1, 1.0) == "a"


def test_cloud_columns_suggestions_check_and_prefill():
    a, v, _ = table(("cost-center", "111111111111", "Alpha-Prod", "1234", 1000.0), ("Project", "222222222222", "Beta-Dev", "P-7", 500.0),
                    ("cost-center", "333333333333", "Gamma", "1111", 200.0))
    dom = T.dominant(v, a)
    own = pd.DataFrame({"account_id": ["111111111111", "", "", ""], "account_name": ["Alpha-Prod", "Beta-Dev", "Gamma", "NotBilling"],
                        "cost_center": ["", "", "2222", ""], "project_code": ["", "", "", ""]})
    s = T.suggestions("AWS", own, "account_name", dom, a, CFG)
    assert s["cost_center"].tolist() == ["1234", "", "1111", ""] and s["project_code"].tolist() == ["", "P-7", "", ""]
    chk = T.check(own, s)
    assert chk[0] == "cost_center: cloud has 1234" and chk[1] == "project_code: cloud has P-7" and chk[2] == "cost_center: differs, cloud says 1111" and chk[3] == ""
    pf = T.prefill(own, s)
    assert pf["cost_center"].tolist() == ["1234", "", "2222", ""]                          # typed value kept, blank filled
    cc = T.cloud_columns("AWS", own, "account_name", ["cost-center"], dom, a)
    assert cc["☁ cost-center"].tolist() == ["1234", "", "1111", ""]


def test_aws_rows_match_by_account_id_when_the_name_differs():
    a, v, _ = table(("Team", "111111111111", "Alpha-Prod", "Ops", 900.0))
    own = pd.DataFrame({"account_id": ["111111111111"], "account_name": ["Alpha-Prod (renamed)"]})
    assert T.resolve_names("AWS", own, "account_name", a).tolist() == ["Alpha-Prod"]


def test_coverage_counts_spend_and_lists_untagged_accounts():
    a, v, _ = table(("cost-center", "111111111111", "Alpha-Prod", "1234", 800.0), ("Project", "222222222222", "Beta-Dev", "P", 500.0))
    dom = T.dominant(v, a)
    c = T.coverage("AWS", "cost_center", dom, a, CFG)
    assert c["share"] == pytest.approx(800 / 1700) and c["with_tag"] == 1 and c["accounts"] == 3
    assert c["untagged"].iloc[0]["account"] == "Beta-Dev"                                  # 500 untagged, then Gamma 200, then Alpha 200
    assert T.coverage("AWS", "project_code", dom, a, CFG)["with_tag"] == 1


def test_save_provider_replaces_only_that_provider(tmp_path, monkeypatch):
    from tests import synth
    monkeypatch.setenv("FINOPS_CONFIG", str(synth.make_env(tmp_path, with_data=False)))
    a, v, c = table(("Team", "111111111111", "Alpha-Prod", "Ops", 900.0))
    T.save_provider("AWS", a, v, c)
    ga, gv, gc = (x.assign(provider="GCP") for x in (a, v, c))
    T.save_provider("GCP", ga, gv, gc)
    T.save_provider("AWS", a.iloc[:1], v.iloc[:0], c)                                      # a refresh with fewer rows
    assert set(T.read_accounts().provider) == {"AWS", "GCP"} and len(T.read_accounts()) == 4 and len(T.read_values()) == 1
    assert T.read_catalog().pulled.dtype == bool


def test_missing_tag_files_are_empty_not_an_error(empty_env):
    assert T.read_accounts().empty and T.read_values().empty and T.read_catalog().empty and not T.has_tags()


# ------------------------------------------------------------------ finance columns in the owner file
def test_owner_tables_have_the_finance_columns_and_treatment_is_checked(empty_env):
    assert E.read_owner_file("GCP").columns.tolist()[-5:-1] == E.FINANCE_COLS
    t = pd.DataFrame({"project": ["p"], "owner": ["a"], "treatment": ["Capex"]})
    errors, warns = E.validate_owners("GCP", t)
    assert not errors and any("treatment" in w for w in warns)
    assert E.validate_owners("GCP", t.assign(treatment="opex")) == ([], [])


def test_finance_edits_are_merged_and_other_columns_kept(empty_env):
    orig = E.read_owner_file("GCP")
    orig = pd.DataFrame({"project": ["p1", "p2"], "family": ["f", "f"], "environment": ["prod", "dev"], "owner": ["a", "b"], "department": ["", ""],
                         "cost_center": ["", ""], "project_code": ["", ""], "treatment": ["", ""], "finance_notes": ["", ""], "extra": ["x", "y"], E.RID: [0.0, 1.0]})
    ed = orig.drop(columns=["extra"]).copy()
    ed.loc[0, ["cost_center", "treatment"]] = ["1234-5678", "Opex"]
    merged, stats = E.merge_owner_edits(orig, ed, {0.0, 1.0}, "GCP")
    assert stats == dict(changed=1, added=0, deleted=0)
    assert merged.loc[0, "cost_center"] == "1234-5678" and merged["extra"].tolist() == ["x", "y"]


# ------------------------------------------------------------------ the page
def page():
    return AppTest.from_file(str(ROOT / "views" / "data_health.py"), default_timeout=120).run()


def test_page_with_tags_shows_cloud_columns_and_the_coverage_panel(tmp_path, monkeypatch):
    from tests import synth
    monkeypatch.setenv("FINOPS_CONFIG", str(synth.make_env(tmp_path)))
    bill = synth.make_billing()
    names = sorted(bill[bill.provider == "GCP"].account.unique())[:2]
    a = pd.DataFrame({"account_id": ["", ""], "account": names, "total": [1000.0, 500.0]})
    v = pd.DataFrame([("cost-center", "", names[0], "1234-5678", 880.0), ("environment", "", names[1], "dev", 400.0)], columns=["key", "account_id", "account", "value", "cost"])
    ta, tv, tc = T.build("GCP", a, v, {"cost-center": "label on the project", "environment": "label on the project"}, "2026-08..2026-10", CFG)
    T.save_provider("GCP", ta, tv, tc)
    (tmp_path / "data" / "owners_gcp.csv").write_text(f"project,family,environment,owner,department\n{names[0]},f,prod,,\n")
    at = page()
    assert not at.exception, [e.value[:300] for e in at.exception]
    assert any(s.value.startswith("Cloud tags") for s in at.subheader)
    m = {x.label: x.value for x in at.metric}
    assert m["cost_center tagged"] == "59% of spend"                                       # 880 of 1500 spend
    assert not any("Cloud tags to show" in ms.label for ms in at.multiselect)               # AWS is selected and has no tags pulled: points to the refresh button
    assert any("No cloud tags pulled for AWS" in i.value for i in at.info)
    at.radio(key="own_prov").set_value("GCP").run()
    assert not at.exception, [e.value[:300] for e in at.exception]
    ms = [x for x in at.multiselect if "Cloud tags to show" in x.label][0]
    assert ms.value == ["cost-center"]                                                      # the mapped key is shown by default


def test_two_projects_with_the_same_display_name_are_one_line_not_a_crash():
    a = pd.DataFrame({"provider": "GCP", "account_id": ["p-1", "p-2", "p-3"], "account": ["Shared Name", "Shared Name", "Other"], "total": [100.0, 300.0, 50.0],
                      "window": "w", "pulled_at": "t"})
    v = pd.DataFrame([("GCP", "cost-center", "p-1", "Shared Name", "1234", 80.0), ("GCP", "cost-center", "p-2", "Shared Name", "1234", 250.0)],
                     columns=["provider", "key", "account_id", "account", "value", "cost"])
    dom = T.dominant(v, a)
    assert len(dom) == 1 and dom.iloc[0].total == 400.0 and dom.iloc[0].coverage == pytest.approx(330 / 400)
    own = pd.DataFrame({"project": ["Shared Name", "Other"], "cost_center": ["", ""], "project_code": ["", ""]})
    assert T.cloud_columns("GCP", own, "project", ["cost-center"], dom, a)["☁ cost-center"].tolist() == ["1234 · 82%", ""]
    c = T.coverage("GCP", "cost_center", dom, a, CFG)
    assert c["accounts"] == 2 and c["with_tag"] == 1 and c["tagged"] == pytest.approx(330.0)
