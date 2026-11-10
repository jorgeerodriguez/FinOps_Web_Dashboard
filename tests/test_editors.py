import os
import time

import numpy as np
import pandas as pd
import pytest

from finops import editors as E
from tests import synth


@pytest.fixture
def data(tmp_path, monkeypatch):
    """A private config + data dir so a test can write owner files / rules without touching the shared synthetic env."""
    monkeypatch.setenv("FINOPS_CONFIG", str(synth.make_env(tmp_path)))
    return tmp_path / "data"


def write_owners(data, text, provider="AWS"):
    p = data / f"owners_{provider.lower()}.csv"
    p.write_text(text, encoding="utf-8")
    return p


AWS = ("account_id,account_name,family,environment,service_name,owner,department,exit_notes\n"
       "000000000123,Acct-Steady,f,prod,,Alice ,,note1\n"
       "724973000000,Acct-Shrunk,f,prod,,,,note2\n"
       "222222222222,Acct-Ended,f,prod,,Bob,,note3\n")


# ------------------------------------------------------------------ reading / writing
def test_read_trims_whitespace_and_keeps_ids_as_text(data):
    write_owners(data, AWS)
    t = E.read_owner_file("AWS")
    assert t.loc[0, "owner"] == "Alice" and t.loc[0, "account_id"] == "000000000123"      # leading zeros survive
    assert list(t[E.RID]) == [0.0, 1.0, 2.0] and "exit_notes" in t.columns


def test_missing_file_is_an_empty_table(data):
    t = E.read_owner_file("GCP")
    assert t.empty and set(E.OWNER_CORE["GCP"]) <= set(t.columns)


def test_save_is_atomic_and_backs_up(data):
    p = write_owners(data, AWS)
    b = E.save_csv(p, pd.read_csv(p, dtype=str).assign(owner="Z"))
    assert b is not None and b.parent.name == "backups" and "Alice" in b.read_text()
    assert not list(data.glob("*.tmp")) and "Z" in p.read_text()


def test_only_the_newest_backups_are_kept(data):
    p = write_owners(data, AWS)
    for i in range(E.KEEP_BACKUPS + 6):
        E.save_csv(p, pd.read_csv(p, dtype=str).assign(owner=str(i)))
    assert len(list((data / "backups").glob("owners_aws.*.csv"))) == E.KEEP_BACKUPS


def test_stale_file_is_detected(data):
    p = write_owners(data, AWS)
    loaded = E.mtime(p)
    E.check_fresh(p, loaded)                                   # unchanged: fine
    time.sleep(0.02)
    p.write_text(AWS + "333333333333,New,f,prod,,Cara,,n\n", encoding="utf-8")
    os.utime(p, (loaded + 5, loaded + 5))
    with pytest.raises(E.Stale):
        E.check_fresh(p, loaded)


# ------------------------------------------------------------------ owners: validation and merge
def test_validate_owner_rules():
    t = pd.DataFrame({"account_id": ["1", "1", "724973000000", ""], "account_name": ["A", "A", "C", ""], "owner": ["x"] * 4})
    errors, warns = E.validate_owners("AWS", t)
    text = " ".join(errors)
    assert "no `account_name`" in text and "Duplicate `account_name`" in text and "Duplicate account_id" in text
    assert any("rounded by Excel" in w for w in warns)
    assert E.validate_owners("GCP", pd.DataFrame({"project": ["p1", "p2"], "owner": ["a", ""]})) == ([], [])


def test_near_duplicate_owner_names():
    s = pd.Series(["Jane Doe"] * 5 + ["jane doe", "Jane Do", "John Smith", "John Smith"])
    pairs = dict(E.near_duplicate_owners(s))
    assert pairs["jane doe"] == "Jane Doe" and pairs["Jane Do"] == "Jane Doe" and "John Smith" not in pairs


def edited_frame(orig, changes):
    e = orig[E.OWNER_CORE["AWS"] + [E.RID]].copy()
    for (i, col), v in changes.items():
        e.loc[i, col] = v
    return e


def test_merge_updates_keeps_unshown_columns_and_counts_changes(data):
    write_owners(data, AWS)
    orig = E.read_owner_file("AWS")
    e = edited_frame(orig, {(1, "owner"): "Carol", (0, "department"): "Eng"})
    out, st = E.merge_owner_edits(orig, e, set(orig[E.RID]), "AWS")
    assert st == dict(changed=2, added=0, deleted=0)
    assert list(out.columns) == [c for c in orig.columns if c != E.RID]
    assert out.loc[1, "owner"] == "Carol" and out.loc[0, "department"] == "Eng"
    assert list(out.exit_notes) == ["note1", "note2", "note3"]                 # the hidden column came back untouched


def test_a_filtered_view_never_deletes_rows_it_did_not_show(data):
    write_owners(data, AWS)
    orig = E.read_owner_file("AWS")
    shown = orig[orig.owner == ""]                                              # only the unassigned row is on screen
    e = shown[E.OWNER_CORE["AWS"] + [E.RID]].copy()
    e.loc[e.index[0], "owner"] = "Dan"
    out, st = E.merge_owner_edits(orig, e, set(shown[E.RID]), "AWS")
    assert len(out) == 3 and st["deleted"] == 0 and out.loc[1, "owner"] == "Dan"
    out, st = E.merge_owner_edits(orig, e.iloc[0:0], set(shown[E.RID]), "AWS")   # the user deleted the shown row
    assert len(out) == 2 and st["deleted"] == 1 and "Acct-Shrunk" not in set(out.account_name)


def test_new_rows_are_appended_and_bare_names_skipped(data):
    write_owners(data, AWS)
    orig = E.read_owner_file("AWS")
    e = orig[E.OWNER_CORE["AWS"] + [E.RID]].copy()
    e.loc[10] = ["", "Brand-New", "fam", "prod", "", "Eve", "", np.nan]
    e.loc[11] = ["", "Bare-Name", "", "", "", "", "", np.nan]
    out, st = E.merge_owner_edits(orig, e, set(orig[E.RID]), "AWS")
    assert st["added"] == 1 and list(out.account_name)[-1] == "Brand-New" and out.iloc[-1]["exit_notes"] == ""
    assert "Bare-Name" not in set(out.account_name)


def test_accounts_missing_from_the_file_are_found_with_their_spend(data, billing):
    write_owners(data, "account_id,account_name,family,environment,service_name,owner,department\n,Acct-Steady,f,prod,,Alice,\n")
    t = E.read_owner_file("AWS")
    spend = E.spend_by_account(billing, "AWS")
    miss = E.missing_accounts(t, spend, "AWS")
    assert set(miss.account_name) == {"Acct-Shrunk", "Acct-Ended", "Acct-Spike"} or set(miss.account_name) == {"Acct-Shrunk", "Acct-Spike"}
    assert miss[E.RID].isna().all()
    rs = E.row_spend(t, spend, "AWS")
    assert rs.iloc[0] == pytest.approx(spend["Acct-Steady"]) and spend["Acct-Steady"] > 0


# ------------------------------------------------------------------ anomaly rules
def test_validate_rules():
    good = pd.DataFrame([dict(provider="AWS", account="A", service="S", months="2026-02;2026-03", action="add_back", note="")])
    assert E.validate_rules(good) == ([], [])
    bad = pd.DataFrame([dict(provider="X", account="", service="S", months="Feb 2026", action="maybe", note=""),
                        dict(provider="AWS", account="A", service="S", months="", action="add_back", note="")])
    errors, _ = E.validate_rules(bad)
    text = " ".join(errors)
    for needle in ("provider", "no account", "action that is not", "months", "add_back row"):
        assert needle in text
    dup = pd.concat([good, good])
    assert E.validate_rules(dup)[1] and "FIRST" in E.validate_rules(dup)[1][0]


def test_suggestions_come_from_pending_anomalies_not_already_in_the_file():
    an = pd.DataFrame([
        dict(Provider="AWS", **{"Account / Project": "A"}, Service="Svc...", Type="Gap", Status="Pending review", _service="Svc full name", _months="2026-02;2026-03", **{"Missing vs typical": 9.0}),
        dict(Provider="AWS", **{"Account / Project": "B"}, Service="Old", Type="Shutdown / fading", Status="Pending review", _service="Old", _months="2026-09", **{"Missing vs typical": 5.0}),
        dict(Provider="GCP", **{"Account / Project": "C"}, Service="Done", Type="Gap", Status="Confirmed real", _service="Done", _months="2026-01", **{"Missing vs typical": 4.0}),
    ])
    rules = pd.DataFrame([dict(provider="AWS", account="B", service="Old", months="2026-09", action="pending", note="")])
    s = E.suggest_rules(an, rules)
    assert list(s.account) == ["A"] and s.iloc[0].service == "Svc full name"          # the FULL service name, so the rule will match
    assert s.iloc[0].action == "add_back" and s.iloc[0].months == "2026-02;2026-03"


def test_a_suggested_rule_really_matches_the_forecast(billing):
    """End to end: a detected gap -> suggested rule -> find_anomalies reports it as reviewed."""
    from finops import nextyear as N
    rows = billing[(billing.category == "Cloud Services") & ~billing.is_partial]
    last = rows.date.max()
    gap_line = pd.DataFrame([dict(date=d, provider="AWS", account="Acct-Gap", service="Amazon Relational Database Service", category="Cloud Services",
                                  env="unknown", cost_gross=v, credits=0.0, cost_net=v, owner="Alice", family="g", is_partial=False)
                             for d, v in zip(pd.date_range(last - pd.DateOffset(months=11), last, freq="MS"), [12_000.0] * 4 + [0.0, 0.0] + [12_000.0] * 6)])
    rows = pd.concat([rows, gap_line], ignore_index=True)
    basis = pd.date_range(rows.date.max() - pd.DateOffset(months=11), rows.date.max(), freq="MS")
    an = N.find_anomalies(rows, basis)
    assert {"_service", "_months"} <= set(an.columns)
    s = E.suggest_rules(an, pd.DataFrame(columns=E.RULE_COLS))
    assert len(s)
    rules = s[E.RULE_COLS].assign(action="confirmed_real")
    again = N.find_anomalies(rows, basis, rules)
    assert (again.Status != "Pending review").sum() >= len(s)


def test_merge_rule_edits_drops_blank_rows_and_keeps_extra_columns(data):
    (data / "anomaly_rules.csv").write_text("provider,account,service,months,action,note,owner_ref\nAWS,A,S,2026-02,PENDING,hi,x\n", encoding="utf-8")
    orig = E.read_rules()
    e = pd.concat([orig[E.RULE_COLS], pd.DataFrame([dict(provider="", account="", service="", months="", action="", note="")])], ignore_index=True)
    out, st = E.merge_rule_edits(orig, e)
    assert len(out) == 1 and out.loc[0, "action"] == "pending" and "owner_ref" in out.columns
    assert st["changed"] == 1                                                    # action lower-cased counts as an edit


# ------------------------------------------------------------------ the screens (AppTest cannot type into a data editor, so test what it can reach)
from pathlib import Path
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent


def page(name):
    return AppTest.from_file(str(ROOT / "views" / f"{name}.py"), default_timeout=120).run()


def test_owner_editor_shows_gaps_and_saving_without_edits_changes_nothing(data):
    write_owners(data, "account_id,account_name,family,environment,service_name,owner,department\n,Acct-Steady,f,prod,,Alice,\n,Acct-Shrunk,f,prod,,,\n")
    before = (data / "owners_aws.csv").read_text()
    at = page("data_health")
    assert not at.exception, [e.value[:200] for e in at.exception]
    m = {x.label: x.value for x in at.metric if x.label in ("Rows in the file", "Without an owner", "Billing but not in the file")}
    assert m["Rows in the file"] == "2" and int(m["Without an owner"]) >= 2 and int(m["Billing but not in the file"]) >= 1
    [b for b in at.button if b.label == "Save owners"][0].click().run()
    assert not at.exception and any("Nothing to save" in i.value for i in at.info)
    assert (data / "owners_aws.csv").read_text() == before and not (data / "backups").exists()            # nothing written, nothing backed up


def test_rules_editor_lists_detected_anomalies_and_saving_without_edits_changes_nothing(data):
    at = page("next_year")
    assert not at.exception, [e.value[:200] for e in at.exception]
    assert any(s.value == "Review anomalies" for s in at.subheader)
    btn = [b for b in at.button if b.label == "Save reviews"]
    assert btn
    btn[0].click().run()
    assert not at.exception and not (data / "anomaly_rules.csv").exists()
