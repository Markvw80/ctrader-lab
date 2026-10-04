"""HTML reports and the LAN web page, on a small real run."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from ctlab.config import env, symbol_spec
from ctlab.engine.backtest import run_backtest
from ctlab.engine.costs import CostModel
from ctlab.report.charts import bar_chart, line_chart, scatter
from ctlab.report.metrics import breakdowns, compute_metrics
from ctlab.runs.registry import save_run
from ctlab.strategy.base import get_strategy
from tests.conftest import make_m1


@pytest.fixture
def runs(tmp_path, monkeypatch):
    monkeypatch.setenv("CTLAB_RESULTS_DIR", str(tmp_path))
    env.cache_clear()
    spec = symbol_spec("XAUUSD", broker=False)
    bars = make_m1(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 2, 1, tzinfo=UTC), seed=4)
    ids = []
    for i, name in enumerate(["mean_reversion", "session_breakout"]):
        res = run_backtest(get_strategy(name)(), bars, CostModel.from_spec(spec), spec["sessions"])
        m = compute_metrics(res, spec["sessions"])
        b = breakdowns(res, spec["sessions"])
        rid = f"20240101-00000{i}-{name}-test"
        save_run(rid, "backtest", {"strategy": name, "symbol": "XAUUSD", "params": res.params,
                                   "sessions": spec["sessions"],
                                   "data": {"rows": bars.height, "first": "2024-01-01",
                                            "last": "2024-02-01", "sha256": "x"},
                                   "engine": {"initial_balance": 10_000}},
                 m, {"trades": res.trades, "equity": res.equity,
                     **{f"breakdown_{k}": v for k, v in b.items()}})
        ids.append(rid)
    yield ids
    env.cache_clear()


def test_charts_render_svg():
    d = [datetime(2024, 1, i, tzinfo=UTC) for i in range(1, 6)]
    assert line_chart([("eq", list(zip(d, [1, 2, 3, 2, 4], strict=True)))]).startswith("<svg")
    assert "class=\"neg\"" in bar_chart(["a", "b"], [1.0, -2.0])
    assert "<circle" in scatter([1, 2, 3], [0.1, 0.5, 0.2], "x", highlight=2)
    assert "no data" in line_chart([("eq", [])])


def test_report_is_self_contained(runs):
    from ctlab.report.html import build_report
    html = build_report(runs[0]).read_text()
    assert "<svg" in html and "Reproducibility" in html and "Per weekday" in html
    assert "<script src" not in html and "https://" not in html   # works offline


def test_web_index_report_compare_csv(runs):
    c = TestClient(__import__("ctlab.web.app", fromlist=["app"]).app)
    r = c.get("/")
    assert r.status_code == 200 and runs[0] in r.text and runs[1] in r.text
    assert c.get(f"/runs/{runs[0]}").status_code == 200
    r = c.get("/compare", params=[("ids", runs[0]), ("ids", runs[1])])
    assert r.status_code == 200 and "Return % over time" in r.text
    r = c.get(f"/runs/{runs[1]}/trades.csv")
    assert r.status_code == 200 and r.text.startswith("signal_ts,")
    assert c.get("/compare", params=[("ids", runs[0])]).status_code == 400
    assert c.get("/?kind=backtest&strategy=mean_reversion").text.count("trades.csv") == 1


@pytest.mark.parametrize("bad", ["..%2F..%2Fetc", "nope", "a" * 200])
def test_web_rejects_unknown_or_unsafe_ids(runs, bad):
    c = TestClient(__import__("ctlab.web.app", fromlist=["app"]).app)
    assert c.get(f"/runs/{bad}").status_code == 404


def test_web_refuses_public_addresses(runs):
    from ctlab.web.app import app
    assert TestClient(app, client=("8.8.8.8", 5000)).get("/").status_code == 403
    assert TestClient(app, client=("192.168.2.20", 5000)).get("/").status_code == 200
    assert TestClient(app, client=("172.18.0.1", 5000)).get("/health").status_code == 200


def test_web_extra_allowed_networks(runs, monkeypatch):
    from ctlab.web.app import app
    assert TestClient(app, client=("100.101.102.103", 5000)).get("/").status_code == 403
    monkeypatch.setenv("CTLAB_WEB_ALLOW_CIDRS", "100.64.0.0/10")
    env.cache_clear()
    assert TestClient(app, client=("100.101.102.103", 5000)).get("/").status_code == 200
    assert TestClient(app, client=("8.8.8.8", 5000)).get("/").status_code == 403
