"""Lifecycle status of accounts / projects: suggestions from billing and audit, adding New accounts, manual changes, purge, the editor and the Orphans page."""
from __future__ import annotations
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from finops import audit as au
from finops import editors as E
from finops import opportunities as O
from finops import status as ST
from finops import store
from tests import synth

ROOT = Path(__file__).resolve().parent.parent
HEAD = "account_id,account_name,family,environment,service_name,owner,department,cost_center,project_code,treatment,finance_notes\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FINOPS_CONFIG", str(synth.make_env(tmp_path)))
    return tmp_path


def write(env, text, provider="AWS"):
    (env / "data" / f"owners_{provider.lower()}.csv").write_text(text)


def table(rows: list[dict]) -> pd.DataFrame:
    t = pd.DataFrame(rows).reindex(columns=E.OWNER_CORE["AWS"]).fillna("")
    t[E.RID] = range(len(t))
    return t


def suggestions(rows, gone=None, quiet=6):
    t = table(rows)
    return ST.suggest("AWS", t, ST.facts("AWS", t, store.read()), gone or {}, quiet)


# ------------------------------------------------------------------ suggestions
def test_canonical_names_and_unknown_values():
    assert ST.canon(" active ") == "Active" and ST.canon("REMOVE") == "Remove" and ST.canon("") == "" and ST.canon("Zzz") == "Zzz"


def test_billing_decides_active_idle_and_deleted(env):
    s = suggestions([dict(account_name="Acct-Steady"), dict(account_name="Acct-Ended"), dict(account_name="Ghost")])
    assert s.suggested.tolist() == ["Active", "Deleted", "Idle"]
    assert "no spend since" in s.why[1] and "never billed" in s.why[2]
    assert suggestions([dict(account_name="Acct-Ended")], quiet=24).suggested.tolist() == ["Active"]      # the threshold is configurable


def test_orphan_and_remove_are_never_suggested_over_but_billing_is_flagged(env):
    s = suggestions([dict(account_name="Acct-Steady", status="Orphan"), dict(account_name="Acct-Ended", status="remove"), dict(account_name="Ghost", status="Orphan")])
    assert s.suggested.tolist() == ["Orphan", "Remove", "Orphan"] and s.same.all()
    assert "still billing" in s.why[0] and s.why[1] == "" and s.why[2] == ""
    assert ST.check_text(s)[0].startswith("marked Orphan but still billing")


def test_new_becomes_active_only_when_owner_code_and_environment_are_filled(env):
    ready = dict(account_name="Acct-Steady", status="New", owner="Alice", project_code="Alpha", environment="prod")
    s = suggestions([ready, {**ready, "owner": ""}, {**ready, "environment": "unspecified"}])
    assert s.suggested.tolist() == ["Active", "New", "New"] and s.same.tolist() == [False, True, True]


def test_idle_or_deleted_rows_that_bill_again_become_active_and_audit_confirms_deletion(env):
    s = suggestions([dict(account_name="Acct-Steady", status="Idle"), dict(account_name="Acct-Steady", status="Deleted"), dict(account_name="Acct-Shrunk", status="Active")],
                    gone={"acct-shrunk": "2026-10-19"})
    assert s.suggested.tolist() == ["Active", "Active", "Deleted"]
    assert "billing is current again" in s.why[0] and "deep audit" in s.why[2]


def test_a_recently_first_billed_row_without_status_is_new(env):
    b = store.read()
    late = b[b.account == "Acct-Spike"].copy()
    late = late[late.date >= late.date.max() - pd.DateOffset(months=1)]
    only = pd.concat([b[b.account != "Acct-Spike"], late])                                          # Acct-Spike first billed last month
    t = table([dict(account_name="Acct-Spike")])
    s = ST.suggest("AWS", t, ST.facts("AWS", t, only), {}, 6)
    assert s.suggested.tolist() == ["New"] and "first billed" in s.why[0]


def test_apply_suggestions_never_touches_manual_statuses(env):
    t = table([dict(account_name="Acct-Steady", status="Orphan"), dict(account_name="Acct-Ended"), dict(account_name="Ghost")])
    out = ST.apply_suggestions(t, suggestions(t.drop(columns=E.RID).to_dict("records")))
    assert out.status.tolist() == ["Orphan", "Deleted", "Idle"] and ST.summary(out)["Idle"] == 1


# ------------------------------------------------------------------ the audit
def test_audit_removed_projects_are_found_by_name_or_id(env):
    au.save_audit("AWS", [dict(name="Acct-Shrunk", id="111", status="gone", state="ACTIVE"), dict(name="X", id="222", status="active", state="SUSPENDED"),
                          dict(name="Y", id="333", status="active", state="ACTIVE")])
    assert set(ST.audit_gone("AWS")) == {"acct-shrunk", "111", "x", "222"}


# ------------------------------------------------------------------ new accounts, manual changes, purge
def test_new_accounts_are_those_billing_recently_and_missing_from_the_file(env):
    write(env, HEAD + ",Acct-Steady,f,prod,,Alice,,,,,\n")
    t = E.read_owner_file("AWS")
    new = ST.new_accounts("AWS", t, store.read())
    assert set(new.account) == {"Acct-Shrunk", "Acct-Spike"}                                       # Acct-Ended stopped 13 months ago: history, not news
    assert set(ST.new_accounts("AWS", t, store.read(), quiet=24).account) >= {"Acct-Ended"}


def test_add_new_writes_status_new_backs_up_logs_and_is_idempotent(env):
    write(env, HEAD + ",Acct-Steady,f,prod,,Alice,,,,,\n")
    added = ST.add_new("AWS")
    assert set(added) == {"Acct-Shrunk", "Acct-Spike"} and (env / "data" / "backups").exists()
    t = E.read_owner_file("AWS")
    assert t.set_index("account_name").loc["Acct-Spike", "status"] == "New" and t.set_index("account_name").loc["Acct-Steady", "status"] == ""
    assert t.columns.tolist().index("status") == t.columns.tolist().index("account_name") + 1       # status sits right after the name column
    log = pd.read_csv(ST.log_path(), dtype=str, keep_default_na=False)
    assert set(log.account) == {"Acct-Shrunk", "Acct-Spike"} and (log.new == "New").all() and (log.source == "refresh").all()
    assert ST.add_new("AWS") == []


def test_aws_new_accounts_named_with_an_id_get_the_id(env):
    b = store.read()
    extra = b[b.account == "Acct-Spike"].assign(account="123456789012 - Acme")
    store.upsert(pd.concat([b[~b.is_partial], extra[~extra.is_partial]]), "AWS")
    write(env, HEAD)
    ST.add_new("AWS")
    t = E.read_owner_file("AWS").set_index("account_name")
    assert t.loc["123456789012 - Acme", "account_id"] == "123456789012"


def test_set_status_updates_adds_rows_and_logs(env):
    write(env, HEAD + ",Acct-Steady,f,prod,,Alice,,,,,\n")
    assert ST.set_status("AWS", ["Acct-Steady", "Brand-New-Row"], "Orphan", "test", "because") == 2
    t = E.read_owner_file("AWS").set_index("account_name")
    assert t.loc["Acct-Steady", "status"] == "Orphan" and t.loc["Brand-New-Row", "status"] == "Orphan" and t.loc["Acct-Steady", "owner"] == "Alice"
    assert ST.set_status("AWS", ["Acct-Steady"], "Orphan", "test") == 0                            # nothing changed, nothing logged
    log = pd.read_csv(ST.log_path(), dtype=str, keep_default_na=False)
    assert len(log) == 2 and set(log.reason) == {"because"} and set(log.old) == {"", ""}


def test_purge_archives_removed_rows_and_leaves_billing_alone(env):
    write(env, HEAD.replace("account_name,", "account_name,status,", 1) + ",Acct-Steady,Active,f,prod,,Alice,,,,,\n,Old-One,Remove,f,prod,,Bob,,,,,\n")
    before = store.read().shape
    assert ST.purge("AWS") == 1
    assert "Old-One" not in set(E.read_owner_file("AWS").account_name)
    arch = list((env / "data" / "archive").glob("owners_aws_removed_*.csv"))
    assert len(arch) == 1 and "Old-One" in arch[0].read_text()
    assert store.read().shape == before and ST.purge("AWS") == 0
    assert (pd.read_csv(ST.log_path(), dtype=str).new == "(purged)").any()


# ------------------------------------------------------------------ the owner files and the billing join
def test_status_column_is_placed_after_the_key_and_unknown_values_warn(env):
    write(env, "project,family,environment,owner,department,cost_center,project_code,treatment,finance_notes,extra\nprj-ramp,f,prod,Cara,,,,,,x\n", "GCP")
    t = E.read_owner_file("GCP")
    assert t.columns.tolist()[:2] == ["project", "status"] and t.columns.tolist()[-2] == "extra"
    errors, warns = E.validate_owners("GCP", t.assign(status="Maybe"))
    assert not errors and any("status" in w for w in warns)
    assert E.validate_owners("GCP", t.assign(status="active")) == ([], [])


def test_status_joins_the_billing_rows(env):
    from finops.data import load_all
    write(env, HEAD.replace("account_name,", "account_name,status,", 1) + ",Acct-Steady,active,f,prod,,Alice,,,,,\n,Acct-Ended,Deleted,f,prod,,Bob,,,,,\n")
    d = load_all().drop_duplicates("account").set_index("account")
    assert d.loc["Acct-Steady", "status"] == "Active" and d.loc["Acct-Ended", "status"] == "Deleted" and d.loc["Acct-Spike", "status"] == "(not set)"


def test_orphans_and_deleted_accounts_that_still_bill_are_a_lead(env):
    from finops.data import load_all
    write(env, HEAD.replace("account_name,", "account_name,status,", 1) + ",Acct-Steady,Orphan,f,prod,,Alice,,,,,\n,Acct-Ended,Deleted,f,prod,,Bob,,,,,\n")
    d = load_all()
    t = O.orphans_billing(d[d.category == "Cloud Services"])
    assert t.account.tolist() == ["Acct-Steady"] and t.status.iloc[0] == "Orphan"                    # Acct-Ended really stopped billing


# ------------------------------------------------------------------ pages
def page(name):
    return AppTest.from_file(str(ROOT / "views" / f"{name}.py"), default_timeout=240).run()


def test_owner_editor_shows_status_check_new_accounts_and_the_new_filters(env):
    write(env, HEAD + ",Acct-Steady,f,prod,,Alice,,,Alpha,,\n,Acct-Ended,f,prod,,Bob,,,,,\n,Ghost,f,prod,,Bob,,,,,\n")
    before = (env / "data" / "owners_aws.csv").read_text()
    at = page("data_health")
    assert not at.exception, [e.value[:300] for e in at.exception]
    opts = at.radio(key="own_AWS_show").options
    assert any(o.startswith("Status needs review") for o in opts) and any(o.startswith("New") for o in opts)
    assert any("bill but are not in the file" in i.value for i in at.info)
    for o in opts:
        at.radio(key="own_AWS_show").set_value(o).run()
        assert not at.exception, (o, [e.value[:300] for e in at.exception])
    at.checkbox(key="own_AWS_applystatus").set_value(True).run()
    assert not at.exception, [e.value[:300] for e in at.exception]
    at.checkbox(key="own_AWS_applystatus").set_value(False).run()
    [b for b in at.button if b.label == "Save owners"][0].click().run()
    assert not at.exception and any("Nothing to save" in i.value for i in at.info)
    assert (env / "data" / "owners_aws.csv").read_text() == before                                  # displaying suggestions, or saving with no edits, writes nothing


def test_saving_with_suggestions_applied_adds_each_missing_account_once_as_new(env):
    write(env, HEAD + ",Acct-Steady,f,prod,,Alice,,,Alpha,,\n,Acct-Ended,f,prod,,Bob,,,,,\n")
    at = page("data_health")
    at.radio(key="own_AWS_show").set_value("All rows").run()
    at.checkbox(key="own_AWS_applystatus").set_value(True).run()
    [b for b in at.button if b.label == "Save owners"][0].click().run()
    assert not at.exception and not at.error, [e.value for e in at.error]
    t = E.read_owner_file("AWS")
    assert t.account_name.tolist().count("Acct-Spike") == 1 and t.set_index("account_name").loc["Acct-Spike", "status"] == "New"
    assert t.set_index("account_name").loc["Acct-Ended", "status"] == "Deleted" and t.set_index("account_name").loc["Acct-Steady", "status"] == "Active"
    log = pd.read_csv(ST.log_path(), dtype=str, keep_default_na=False)
    assert {"Acct-Steady", "Acct-Ended", "Acct-Spike"} <= set(log.account) and (log.source == "editor").all()


def test_an_unrelated_save_never_writes_the_proposed_accounts_as_blank_rows(env):
    write(env, HEAD + ",Acct-Steady,f,prod,,Alice,,,Alpha,,\n")
    at = page("data_health")
    at.radio(key="own_AWS_show").set_value("All rows").run()
    at.checkbox(key="own_AWS_applystatus").set_value(True).run()
    at.checkbox(key="own_AWS_applystatus").set_value(False).run()
    from finops.editors import merge_owner_edits
    t = E.read_owner_file("AWS")
    edited = t.copy()
    edited.loc[0, "owner"] = "Zoe"
    prop = pd.DataFrame([{c: "" for c in t.columns} | {"account_name": "Acct-Spike", E.RID: float("nan")}])           # a proposed row, left blank
    merged, stats = merge_owner_edits(t, pd.concat([edited, prop], ignore_index=True), set(t[E.RID]), "AWS")
    assert merged.account_name.tolist() == ["Acct-Steady"] and stats["changed"] == 1 and stats["added"] == 0
    prop.loc[0, "owner"] = "Eve"                                                                                        # filled in: written once
    merged, stats = merge_owner_edits(t, pd.concat([edited, prop], ignore_index=True), set(t[E.RID]), "AWS")
    assert merged.account_name.tolist() == ["Acct-Steady", "Acct-Spike"] and stats["added"] == 1


def test_add_new_button_writes_new_rows(env):
    write(env, HEAD + ",Acct-Steady,f,prod,,Alice,,,Alpha,,\n")
    at = page("data_health")
    [b for b in at.button if b.label == "Add them to the file as New"][0].click().run()
    assert not at.exception, [e.value[:300] for e in at.exception]
    assert {"Acct-Shrunk", "Acct-Spike"} <= set(E.read_owner_file("AWS").account_name)


def test_orphans_page_marks_rows_and_audit_confirmed_deletions(env):
    write(env, HEAD + ",Acct-Steady,f,prod,,Alice,,,Alpha,,\n,Acct-Ended,f,prod,,Bob,,,,,\n")
    au.save_audit("AWS", [dict(name="Acct-Ended", id="1", status="gone", state="SUSPENDED", action="x", cost12=0, avg3=0, services=[], months=[], month_labels=[])])
    at = page("orphans")
    assert not at.exception, [e.value[:300] for e in at.exception]
    ms = [m for m in at.multiselect if m.key == "mark_orphan_AWS"]
    assert ms and "Acct-Ended" in ms[0].options
    ms[0].set_value(["Acct-Ended"]).run()
    [b for b in at.button if b.key == "mark_orphan_btn_AWS"][0].click().run()
    assert E.read_owner_file("AWS").set_index("account_name").loc["Acct-Ended", "status"] == "Orphan"
    [b for b in at.button if b.key == "mark_deleted_AWS"][0].click().run()
    assert E.read_owner_file("AWS").set_index("account_name").loc["Acct-Ended", "status"] == "Deleted"
