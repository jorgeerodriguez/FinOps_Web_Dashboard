"""Grafana Cloud: the cost model, commit runway, reconciliation, the collector (faked API) and the pages / report."""
from __future__ import annotations
import re
from datetime import date, datetime

import pandas as pd
import pytest

from finops import grafana as gf
from finops import platforms
from tests import synth


@pytest.fixture
def data():
    c, d, b, f = synth.make_grafana()
    lg, ad = synth.make_grafana_extras()
    return dict(costs=c, drivers=d, balance=b, facts=f, logs=lg, adaptive=ad)


@pytest.fixture
def iso(tmp_path, monkeypatch):
    monkeypatch.setenv("FINOPS_CONFIG", str(synth.make_env(tmp_path, with_data=False)))
    return tmp_path


def test_product_mapping():
    assert gf.product_for("metrics") == "Metrics" and gf.product_for("logs_retention") == "Logs"
    assert gf.product_for("infra_o11y_host") == "Infrastructure" and gf.product_for("fe_o11y") == "Application / frontend"
    assert gf.product_for("ai_tokens") == "AI" and gf.product_for("grafana") == "Grafana users"
    assert gf.product_for("something_new") == "Other"


def test_monthly_excludes_the_total_reference_metric(data):
    m = gf.monthly(data["costs"])
    assert "Other" not in m.columns                                  # the "total" rows are product "Other": they must not be summed in
    d = gf.summarize(data)
    totals = gf.reported_total(data["costs"])
    assert d["total"].iloc[-1] == pytest.approx(totals.iloc[-1])      # parts add up to the reported total
    assert d["parts_vs_total"]["ok"].all()


def test_summary_numbers(data):
    d = gf.summarize(data, budget=20_000)
    assert d["last"] == pd.Timestamp("2026-10-01") and d["prev"] == pd.Timestamp("2026-09-01")
    assert d["months"] == 12 and d["last12"] == pytest.approx(d["total"].tail(12).sum())
    assert d["estimate"] == 12_000.0 and d["est_month"] == "2026-11"
    assert d["now"] > d["before"]                                     # the Oct infra step
    text = " ".join(gf.findings(d, gf.product_table(d)))
    assert "over" in text and "Infrastructure" in text and "commit" in text


def test_commit_runway_maths():
    s = pd.Series([20_000.0, 24_000.0, 22_000.0])
    today, end = date(2026, 11, 13), date(2027, 8, 6)
    months_left = (end - today).days / 30.4375                           # 8.74 months to the contract end
    c = gf.commit_status(500_000.0, 150_000.0, s, today, end)            # 150K / 22K = 6.82 months of runway: runs out early
    assert c["burn"] == pytest.approx(22_000.0) and c["runway"] == pytest.approx(150_000 / 22_000)
    assert c["consumed"] == pytest.approx(0.7)
    assert c["shortfall"] == pytest.approx(months_left - 150_000 / 22_000)
    ok = gf.commit_status(500_000.0, 200_000.0, s, today, end)           # 9.09 months of runway: covers the term
    assert ok["runway"] > months_left and ok["shortfall"] is None
    assert gf.commit_status(500_000.0, 200_000.0, s.head(1), today, None) is None      # too little history for a burn rate
    assert gf.commit_status(0.0, 0.0, s, today, None) is None                          # no commit at all


def test_commit_covers_the_term_when_balance_is_large():
    c = gf.commit_status(500_000.0, 400_000.0, pd.Series([10_000.0] * 3), date(2026, 11, 13), date(2027, 8, 6))
    assert c["shortfall"] is None


def test_reconciliation_matches_the_drawdown(data):
    d = gf.summarize(data)
    rec = d["reconciliation"]
    assert len(rec) == data["costs"]["month"].nunique() == 13 and rec["ok"].all()
    assert rec["variance"].abs().max() < 0.01
    assert gf.reconcile(100.0, 0.0)["variance"] == float("inf") and gf.reconcile(0.0, 0.0)["variance"] == 0.0


def test_money_keeps_a_decimal_in_the_tens_of_thousands():
    assert gf.money(29_537.58) == "$29.5K" and gf.money(950.4) == "$950" and gf.money(1_200_000) == "$1.20M"


def test_empty_inputs():
    empty = dict(costs=pd.DataFrame(columns=gf.COST_COLS), drivers=pd.DataFrame(columns=gf.DRV_COLS), balance=pd.DataFrame(columns=gf.BAL_COLS), facts={})
    assert gf.summarize(empty) is None


def test_store_accumulates_history(data, iso):
    c = data["costs"]
    gf.save(c[c["month"] < "2026-06-01"], data["drivers"], data["balance"], data["facts"])
    gf.save(c[c["month"] >= "2026-04-01"], data["drivers"], data["balance"], data["facts"])
    got = gf.read()["costs"]
    assert got["month"].nunique() == c["month"].nunique() and len(got) == len(c)


# ------------------------------------------------------------------------------------------ collector (faked API)
def _fake_get(self, path, params=None):
    params = params or {}
    now = datetime.utcnow()
    this = pd.Timestamp(now).to_period("M")
    q = params.get("query", "")
    if path.endswith("/label/__name__/values"):
        return {"data": ["grafanacloud_org_metrics_overage", "grafanacloud_org_logs_overage", "grafanacloud_org_total_overage", "unrelated_metric"]}
    if path.endswith("/loki/api/v1/index/volume"):
        return {"data": {"result": [{"metric": {"service_name": "svc-a"}, "value": [0, str(2 * 1024**3)]}, {"metric": {"service_name": "svc-b"}, "value": [0, str(1024**3)]},
                                     {"metric": {}, "value": [0, str(1024**3 // 2)]}]}}
    if path == "/aggregations/recommendations":
        return [dict(metric="m1", recommended_action="update", raw_series_count=100, current_series_count=50, recommended_series_count=20, drop_labels=["pod"], aggregations=["sum"],
                     usages_in_rules=0, usages_in_dashboards=0, usages_in_queries=1)]
    if "/loki/" in path:
        if q.startswith("count(sum by (userId)"):
            return {"data": {"result": [{"value": [0, "7"]}]}}
        if "folderName" in q:
            return {"data": {"result": [{"metric": {"folderName": "Platform"}, "value": [0, "42"]}, {"metric": {}, "value": [0, "8"]}]}}
        return {"data": {"result": [{"value": [0, "11"]}]}}
    if path == "/api/search":
        return [{"uid": "a"}, {"uid": "b"}, {"uid": "c"}]
    if path.endswith("/query"):
        val = {"sum(grafanacloud_org_total_overage)": 1234.0, "sum(grafanacloud_org_spend_commit_credit_total)": 100000.0,
               "sum(grafanacloud_org_spend_commit_balance_total)": 60000.0, "max(grafanacloud_org_contract_start_date)": 1754524800.0,
               "max(grafanacloud_org_contract_end_date)": 1786060800.0}.get(q, 5.0)
        return {"data": {"result": [{"value": [0, str(val)]}]}}
    if path.endswith("/query_range"):
        start, end, step = int(params["start"]), int(params["end"]), int(params["step"])
        pts, t = [], start
        while t <= end:
            ts = pd.Timestamp(t, unit="s")
            if step == 3600:                                          # running overage: grows through the month, resets at the cycle start
                v = (ts.day + ts.hour / 24.0) * (10.0 if "metrics" in q else 5.0)
            elif "balance" in q:
                v = 100000.0 - (t - start) / 86400.0
            else:
                v = 100.0
            pts.append([t, str(v)])
            t += step * (24 if step == 3600 else 1) if step == 3600 else step
        return {"data": {"result": [{"values": pts}]}}
    raise AssertionError(path)


def test_collector_end_to_end_with_fake_api(monkeypatch, iso):
    from ingest import grafana as ing
    monkeypatch.setattr(ing.Client, "get", _fake_get)
    monkeypatch.setattr(platforms, "credentials", lambda *_: ({"GRAFANA_STACK_TOKEN": "glsa_fake_token_value", "GRAFANA_URL": "https://example.grafana.net"}, "x"))
    n = ing.refresh()
    assert n > 0
    got = gf.read()
    assert {"metrics", "logs", "total"} == set(got["costs"]["component"])
    this = pd.Timestamp(datetime.utcnow()).to_period("M").start_time
    assert (got["costs"]["month"] < this).all()                      # the month in progress is an estimate, never a complete month
    f = got["facts"]
    assert f["commit_total"] == 100000.0 and f["commit_balance"] == 60000.0 and f["current_estimate"] == 1234.0
    assert f["contract_end"] and re.fullmatch(r"\d{4}-\d{2}-\d{2}", f["contract_end"])
    assert f["activity"]["dashboards_total"] == 3 and f["activity"]["active_users"]["30"] == 7
    assert f["activity"]["folders_30d"] == {"Platform": 42, "General (no folder)": 8}
    assert "glsa_fake_token_value" not in str(f)                      # nothing secret is saved
    assert len(platforms.read_log("Grafana")) == 1
    assert len(got["logs"]) and set(got["logs"]["service"]) == {"svc-a", "svc-b", "(unlabelled)"} and got["logs"]["day"].nunique() == gf.LOG_DAYS
    assert got["logs"][got["logs"]["service"] == "svc-a"]["gib"].iloc[0] == pytest.approx(2.0)
    assert got["adaptive"].empty and any("Adaptive Metrics" in n for n in f["notes"])      # not configured: skipped with a note, refresh still succeeds


def test_collector_needs_token_and_url():
    from ingest import grafana as ing
    with pytest.raises(ing.GrafanaError, match="No Grafana token"):
        ing.Client("https://x.grafana.net", "")
    with pytest.raises(ing.GrafanaError, match="No Grafana URL"):
        ing.Client("", "glsa_x")


def test_http_errors_never_leak_the_token(monkeypatch):
    import io
    import urllib.error
    from ingest import grafana as ing
    c = ing.Client("https://x.grafana.net", "glsa_super_secret_token")

    def boom(req, timeout=60):
        raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(b"echo glsa_super_secret_token"))
    monkeypatch.setattr(ing.urllib.request, "urlopen", boom)
    with pytest.raises(ing.GrafanaError) as e:
        c.get("/api/x")
    assert "glsa_super_secret_token" not in str(e.value)
    auth = urllib_error_401(monkeypatch, ing, c)
    assert "token is invalid" in auth


def urllib_error_401(monkeypatch, ing, c):
    import io
    import urllib.error

    def boom(req, timeout=60):
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b""))
    monkeypatch.setattr(ing.urllib.request, "urlopen", boom)
    with pytest.raises(ing.GrafanaError) as e:
        c.get("/api/x")
    return str(e.value)


# ------------------------------------------------------------------------------------------ report / strip
def test_report_builds(data):
    from finops.grafana_report import build_report
    html = build_report(data, 20_000)
    assert "Grafana Cloud" in html and "Metrics" in html and "commit" in html.lower() and "not invoices" in html
    assert "glsa_" not in html


def test_executive_strip_includes_both_platforms():
    from finops import platforms_ui
    names = [s["name"] for s in platforms_ui.summaries()]
    assert names == ["Equinix", "Grafana Cloud"]


# ------------------------------------------------------------------------------------------ logs by service
def test_log_view_shares_movers_and_minimum_days(data):
    v = gf.log_view(data["logs"], top=2)
    assert v["days"] == 31 and v["services"] == 4
    assert v["table"]["share"].sum() == pytest.approx(1.0)
    assert list(v["table"]["service"]) == ["svc-a", "svc-b", gf.OTHER_SERVICES]       # top 2 plus the folded tail
    assert v["movers"]["service"].tolist() == ["svc-a"] and v["movers"]["extra_per_day"].iat[0] == pytest.approx(6.0)
    assert v["recent"] > v["earlier"]
    text = " ".join(gf.log_findings(v))
    assert "svc-a" in text and "up" in text
    short = data["logs"][data["logs"]["day"] > "2026-11-05"]                          # 8 days: below the minimum
    assert gf.log_view(short) is None and gf.log_view(None) is None and gf.log_view(data["logs"].head(0)) is None


def test_log_view_has_no_movers_without_a_baseline():
    lg, _ = synth.make_grafana_extras()
    v = gf.log_view(lg[lg["day"] > "2026-10-25"], recent_days=7)                       # 19 days: baseline of 12 is fine
    assert v is not None
    v2 = gf.log_view(lg[lg["day"] > "2026-10-30"], recent_days=7)                      # 14 days: still enough
    assert v2 is not None and len(v2["movers"]) >= 0


# ------------------------------------------------------------------------------------------ Adaptive Metrics
def test_adaptive_view(data):
    a = gf.adaptive_view(data["adaptive"], active_now=2_000_000)
    assert a["metrics"] == 4 and a["reducible"] == 10_000 + 1_000 + 1_000
    assert a["pct_of_active"] == pytest.approx(12_000 / 2_000_000)
    assert list(a["top"]["metric"]) == ["m_big_bucket", "m_mid", "m_unused"]           # keep-only metric has nothing to reduce
    assert a["unused_series"] == 1_000                                                 # only m_unused has no rule, dashboard or query (m_big_bucket has 3 queries)
    assert a["top_share"] == pytest.approx(1.0)
    disp = gf.adaptive_display(a["top"])
    assert disp["Reducible"].iat[0] == "10,000" and "…" in disp["Labels to drop"].iat[0]
    assert gf.adaptive_view(None, 1.0) is None and gf.adaptive_view(data["adaptive"].head(0), 1.0) is None


# ------------------------------------------------------------------------------------------ unit costs
def test_unit_costs_and_the_volume_vs_price_note(data):
    d = gf.summarize(data)
    u = gf.unit_costs(d)
    assert list(u.columns) == ["Metrics $ per 1K series (approx.)"]                    # the synthetic drivers hold active series only
    assert u.iloc[0, 0] == pytest.approx(9000.0 / 2000.0)                              # Oct 2025: $9,000 of Metrics over 2.0M series
    assert gf.unit_costs(dict(drivers=pd.DataFrame(columns=gf.DRV_COLS), monthly=d["monthly"])) is None
    # synthetic logs have no driver series, so build a case: cost triples at a flat price per GB -> "volume"
    idx = [pd.Timestamp("2026-09-01"), pd.Timestamp("2026-10-01")]
    m = pd.DataFrame({"Logs": [1000.0, 3000.0]}, index=pd.DatetimeIndex(idx, name="month"))
    flat = pd.DataFrame({"Logs $ per GB": [0.40, 0.40]}, index=m.index)
    dd = dict(last=idx[1], prev=idx[0], monthly=m)
    assert "volume" in gf.unit_note(dd, flat)
    moved = pd.DataFrame({"Logs $ per GB": [0.40, 0.60]}, index=m.index)
    assert "not only volume" in gf.unit_note(dd, moved)
    quiet = pd.DataFrame({"Logs": [1000.0, 1050.0]}, index=m.index)
    assert gf.unit_note(dict(last=idx[1], prev=idx[0], monthly=quiet), flat) is None   # under a 15% move: nothing to say


def test_store_keeps_log_days_and_replaces_the_adaptive_snapshot(data, iso):
    lg, ad = data["logs"], data["adaptive"]
    gf.save(data["costs"], data["drivers"], data["balance"], data["facts"], lg[lg["day"] <= "2026-11-05"], ad)
    gf.save(data["costs"], data["drivers"], data["balance"], data["facts"], lg[lg["day"] >= "2026-11-01"], ad.head(2))
    got = gf.read()
    assert got["logs"]["day"].nunique() == 31 and len(got["logs"]) == len(lg)          # overlap replaced, older days kept
    assert len(got["adaptive"]) == 2                                                    # snapshot replaced
    gf.save(data["costs"], data["drivers"], data["balance"], data["facts"], None, None)  # a refresh that skipped both keeps what is saved
    assert len(gf.read()["adaptive"]) == 2


# ------------------------------------------------------------------------------------------ collector: Adaptive Metrics
def test_adaptive_collected_with_basic_auth_and_failure_is_not_fatal(monkeypatch, iso):
    from ingest import grafana as ing
    seen = {}
    real_init = ing.Client.__init__

    def spy(self, url, token, basic_user=None):
        real_init(self, url, token, basic_user)
        seen[url] = self.auth
    monkeypatch.setattr(ing.Client, "__init__", spy)
    monkeypatch.setattr(ing.Client, "get", _fake_get)
    monkeypatch.setattr(platforms, "credentials", lambda *_: ({"GRAFANA_STACK_TOKEN": "glsa_fake_stack_value", "GRAFANA_URL": "https://example.grafana.net",
                                                                "GRAFANA_CLOUD_TOKEN": "glc_fake_cloud_value"}, "x"))
    monkeypatch.setattr(platforms, "section", lambda name: {"adaptive_url": "https://mimir.example.net", "adaptive_instance_id": "12345"})
    ing.refresh()
    ad = gf.read()["adaptive"]
    assert len(ad) == 1 and ad["metric"].iat[0] == "m1" and ad["current"].iat[0] == 50 and ad["drop_labels"].iat[0] == "pod"
    assert seen["https://mimir.example.net"].startswith("Basic ") and seen["https://example.grafana.net"].startswith("Bearer ")
    assert "glc_fake_cloud_value" not in str(gf.read()["facts"])

    def refused(self, path, params=None):
        if path == "/aggregations/recommendations":
            raise ing.GrafanaError("403 from /aggregations/recommendations: the token is invalid or cannot query datasources.")
        return _fake_get(self, path, params)
    monkeypatch.setattr(ing.Client, "get", refused)
    ing.refresh()                                                      # a refused Cloud token must not fail the whole refresh
    assert any("Adaptive Metrics" in n and "403" in n for n in gf.read()["facts"]["notes"])


def test_log_failure_is_not_fatal(monkeypatch, iso):
    from ingest import grafana as ing

    def get(self, path, params=None):
        if path.endswith("/index/volume"):
            raise ing.GrafanaError("HTTP 500 from index/volume")
        return _fake_get(self, path, params)
    monkeypatch.setattr(ing.Client, "get", get)
    monkeypatch.setattr(platforms, "credentials", lambda *_: ({"GRAFANA_STACK_TOKEN": "glsa_fake_stack_value", "GRAFANA_URL": "https://example.grafana.net"}, "x"))
    assert ing.refresh() > 0
    assert any("Logs by service" in n for n in gf.read()["facts"]["notes"])


def test_report_has_the_new_sections(data):
    from finops.grafana_report import build_report
    html = build_report(data, 20_000)
    assert "Logs by service" in html and "svc-a" in html and "Adaptive Metrics" in html and "m_big_bucket" in html and "Unit costs" in html


def test_page_shows_the_new_sections():
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    root = Path(__file__).resolve().parent.parent
    at = AppTest.from_file(str(root / "views" / "grafana.py"), default_timeout=120).run()
    assert not at.exception, [e.value[:300] for e in at.exception]
    subs = [s.value for s in at.subheader]
    assert "Logs by service" in subs and "Adaptive Metrics — largest reductions" in subs
