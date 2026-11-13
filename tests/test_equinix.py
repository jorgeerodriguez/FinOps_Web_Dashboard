"""Equinix: normalising the billing payloads, the analysis, the store, the collector (faked API) and the credentials handling."""
from __future__ import annotations
import pandas as pd
import pytest

from finops import equinix as eq
from finops import platforms
from tests import synth


@pytest.fixture
def iso(tmp_path, monkeypatch):
    """A private empty data folder, so store tests never share state with the session's synthetic data."""
    monkeypatch.setenv("FINOPS_CONFIG", str(synth.make_env(tmp_path, with_data=False)))
    return tmp_path


@pytest.fixture
def tables():
    return eq.normalize(*synth.make_equinix_raw())


def test_normalize_names_categories_and_credits(tables):
    ln, inv = tables
    assert set(ln.columns) == set(eq.LINE_COLS) and set(inv.columns) == set(eq.INV_COLS)
    assert {"Lab-FW-Node0", "Lab-FW-Node1", "lab-router", "lab-aws-primary"} <= set(ln["item"])      # uuid -> name
    assert set(ln["category"]) == {"Network Edge", "AWS", "Cloud Router", "Internet Access"}
    cr = ln[ln["kind"] == "credit"]
    assert len(cr) == 1 and cr["amount"].iat[0] == -5000.0
    assert (ln[ln["kind"] == "charge"]["amount"] > 0).all()


def test_normalize_keeps_only_cost_fields():
    raw = synth.make_equinix_raw()
    raw[1][0]["contacts"] = [{"email": "x@y.z"}]
    raw[1][0]["customerDetails"] = {"name": "private"}
    ln, _ = eq.normalize(*raw)
    assert "contacts" not in ln.columns and "customerDetails" not in ln.columns
    assert "private" not in ln.to_csv() and "x@y.z" not in ln.to_csv()


def test_summary_numbers(tables):
    d = eq.summarize(*tables)
    assert d["last"] == pd.Timestamp("2026-11-01") and d["prev"] == pd.Timestamp("2026-10-01")
    assert d["now"] == pytest.approx(2200 + 500 + 1400 + 160)
    assert d["before"] == pytest.approx(2200 + 500 + 1200 + 2200)
    assert d["run_rate"] == pytest.approx(d["now"])                          # latest INVOICE recurring
    assert d["credits12"] == -5000.0 and d["months"] == 12
    assert d["last12"] == pytest.approx(d["total"].tail(12).sum())            # credits not netted


def test_credits_are_never_netted_into_charges(tables):
    d = eq.summarize(*tables)
    m = d["monthly"]
    assert (m.values >= 0).all()
    credit_month = pd.Timestamp("2026-06-01")
    assert m.loc[credit_month].sum() == pytest.approx(2200 + 500 + 1200 + 2200)


def test_changes_new_ended_higher(tables):
    ln, inv = tables
    d = eq.summarize(ln, inv)
    c = eq.changes(ln, d["last"], d["prev"]).set_index("item")["status"].to_dict()
    assert c == {"Lab-FW-Node1": "Ended", "Internet Access 1G": "New", "lab-router": "Higher"}


def test_findings_and_kpis_mention_the_basics(tables):
    d = eq.summarize(*tables)
    items = eq.latest_items(tables[0], d["last"])
    chg = eq.changes(tables[0], d["last"], d["prev"])
    text = " ".join(eq.findings(d, items, chg))
    assert "credits" in text.lower() and "ended" in text and "new line" in text
    assert len(eq.kpi_items(d)) == 5


def test_empty_inputs_do_not_crash():
    ln, inv = eq.normalize([], [])
    assert eq.summarize(ln, inv) is None
    assert eq.monthly(ln).empty


def test_store_accumulates_history(tables, iso):
    ln, inv = tables
    first = ln[ln["month"] < pd.Timestamp("2026-06-01")]
    eq.save(first, inv[inv["month"] < pd.Timestamp("2026-06-01")])
    eq.save(ln[ln["month"] >= pd.Timestamp("2026-04-01")], inv[inv["month"] >= pd.Timestamp("2026-04-01")])   # overlap, newer window
    got, _ = eq.read()
    assert got["month"].nunique() == ln["month"].nunique()                       # older months kept, overlap replaced not doubled
    assert len(got) == len(ln)


# ------------------------------------------------------------------------------------------ collector (faked API)
def test_collector_end_to_end_with_fake_api(monkeypatch, iso):
    from ingest import equinix as ing
    inv, lines, names = synth.make_equinix_raw()
    calls = []

    def fake(self, path, body=None, method=None, token=None, timeout=90):
        calls.append(path.split("?")[0])
        if path.startswith("/oauth2"):
            return {"access_token": "tok-123456789"}
        if path.startswith("/fabric/v4/connections/search"):
            return {"data": [{"uuid": "00000004-0000-0000-0000-000000000000", "name": "lab-aws-primary", "account": {"accountNumber": "999"}}], "pagination": {"total": 1}}
        if path.startswith("/fabric/v4/routers/search") or path.startswith("/ne/v1/devices"):
            return {"data": [], "pagination": {"total": 0}}
        if path.startswith("/v2/invoices/details"):
            return {"data": lines, "pagination": {"total": len(lines)}}
        if path.startswith("/v2/invoices"):
            return {"data": inv}
        raise AssertionError(path)

    monkeypatch.setattr(ing.Client, "_call", fake)
    monkeypatch.setattr(platforms, "credentials", lambda *_: ({"EQUINIX_CLIENT_ID": "id", "EQUINIX_CLIENT_SECRET": "secret-xyz-123"}, "x"))
    n_lines, n_inv = ing.refresh()
    assert n_lines == len(lines) and n_inv == len(inv)
    assert "/oauth2/v1/token" in calls
    got, _ = eq.read()
    assert len(got) == len(lines)
    log = platforms.read_log("Equinix")
    assert len(log) == 1 and log["rows"].iat[0] == len(lines)


def test_collector_errors_never_leak_secrets():
    from ingest import equinix as ing
    c = ing.Client.__new__(ing.Client)
    c.secrets = ["super-secret-value"]
    assert "super-secret-value" not in platforms.redact("HTTP 401 bad super-secret-value here", c.secrets)


def test_missing_credentials_message(monkeypatch):
    from ingest import equinix as ing
    with pytest.raises(ing.EquinixError, match="No Equinix credentials"):
        ing.Client({})


# ------------------------------------------------------------------------------------------ credentials
def test_env_file_parsing_and_precedence(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("# c\nA_KEY = 'from-file'\nB_KEY=\"b\"\nbad line\n")
    assert platforms.load_env_file(f) == {"A_KEY": "from-file", "B_KEY": "b"}
    assert platforms.load_env_file(tmp_path / "missing") == {}
    monkeypatch.setattr(platforms, "section", lambda name: {"env_file": str(f)})
    monkeypatch.setenv("A_KEY", "from-env")
    vals, _ = platforms.credentials("equinix", ["A_KEY", "B_KEY", "C_KEY"])
    assert vals == {"A_KEY": "from-env", "B_KEY": "b", "C_KEY": ""}


def test_report_builds(tables):
    from finops.equinix_report import build_report
    html = build_report(*tables)
    assert "Equinix" in html and "Credits" in html and "Lab-FW-Node0" in html
    assert "secret" not in html.lower().replace("secrets", "")


def test_executive_strip_summary():
    from finops import platforms_ui
    s = platforms_ui.summaries()
    assert s and s[0]["name"] == "Equinix"
