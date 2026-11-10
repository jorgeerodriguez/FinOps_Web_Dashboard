import pandas as pd
import pytest

from finops import history as H


@pytest.fixture
def h(cloud):
    return H.compute(cloud)


def test_windows_and_totals(h, cloud):
    assert h.has_prev and len(h.cur) == 12 and len(h.prev) == 12
    assert h.cur[-1] == h.last and h.prev[-1] == h.cur[0] - pd.DateOffset(months=1)
    cur, prev = H.r12(h)
    assert cur == pytest.approx(cloud[cloud.date.isin(h.cur)].cost_net.sum())
    assert prev == pytest.approx(cloud[cloud.date.isin(h.prev)].cost_net.sum())


def test_too_short_history_has_no_comparison(cloud):
    h = H.compute(cloud[cloud.date >= cloud.date.max() - pd.DateOffset(months=15)])
    assert not h.has_prev
    assert H.movers(h) .empty and H.story(h, "service") == [] and H.fig_bridge(h) is None


def test_spike_found_and_rebound_not_reported(h):
    up = h.spikes[h.spikes.direction == "Up"]
    assert len(up) == 1 and up.iloc[0].provider == "AWS" and up.iloc[0].difference == pytest.approx(190_000)
    assert "Amazon Elastic Compute Cloud" in up.iloc[0].driver
    assert not ((h.spikes.direction == "Down") & (h.spikes.month == up.iloc[0].month + pd.DateOffset(months=1))).any()


def test_movers_add_up_to_the_net_change(h):
    m = H.movers(h, "account", 1000)
    cur, prev = H.r12(h)
    assert m.change.sum() == pytest.approx(cur - prev)
    new = m[m.status == "New"]
    assert (new.prior < 1000).all()


def test_story_net_change_and_migration_caveat(h):
    lines = H.story(h, "account")
    cur, prev = H.r12(h)
    assert f"{abs(cur - prev) / 1e3:,.0f}K" in lines[0] or "M" in lines[0]
    assert any("moved between clouds" in x for x in lines)           # AWS shrinks while GCP ramps
    assert any("one-off" in x.lower() for x in H.story(h, "service"))


def test_kpis_and_narrative_render(h, billing):
    assert len(H.kpi_items(h)) == 5
    assert H.narrative(h)
    H.fig_trend(h), H.fig_yoy(h), H.fig_rolling(h), H.fig_bridge(h), H.spikes_display(h), H.movers_display(H.movers(h))
    H.separate_summary(billing[~billing.is_partial], h), H.fig_marketplace(billing[~billing.is_partial], h)


def test_lookback_uses_only_older_data(billing):
    b = billing[~billing.is_partial]
    lb = H.lookback(b)
    assert lb is not None and len(lb["window"]) == 12 and lb["window"][-1] == b.date.max()
    svc = b[(b.category == "Cloud Services") & b.date.isin(lb["window"])]
    assert lb["total"]["Actual"] == pytest.approx(svc.cost_net.sum())
    assert lb["total"]["Low"] <= lb["total"]["Base"] <= lb["total"]["High"]
    H.lookback_table(lb), H.fig_lookback(lb), H.lookback_text(lb)


def test_lookback_needs_two_years(billing):
    short = billing[billing.date >= billing.date.max() - pd.DateOffset(months=20)]
    assert H.lookback(short[~short.is_partial]) is None
