"""Read-only LAN web page: list runs, open reports, compare runs side by side.

Requests from non-private addresses are refused (defence in depth; the port should only be
reachable inside the local network anyway).
"""

import ipaddress
import re

import polars as pl
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from ctlab import __version__
from ctlab.config import env
from ctlab.report.charts import line_chart
from ctlab.report.html import build_report, jinja_env
from ctlab.runs.registry import list_runs, load_run, runs_dir

app = FastAPI(title="ctrader-lab", docs_url=None, redoc_url=None, openapi_url=None)
RUN_ID = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
MAX_COMPARE = 6

COMPARE_ROWS = [
    ("trades", "Trades", None), ("net_profit", "Net result", "max"), ("return_pct", "Return %", "max"),
    ("profit_factor", "Profit factor", "max"), ("sharpe", "Sharpe", "max"),
    ("max_drawdown_pct", "Max DD %", "min"), ("win_rate_pct", "Win rate %", "max"),
    ("avg_trade", "Avg trade", "max"), ("avg_r", "Avg R", "max"),
]


def _extra_networks() -> list:
    raw = env().ctlab_web_allow_cidrs
    return [ipaddress.ip_network(c.strip(), strict=False) for c in raw.split(",") if c.strip()]


@app.middleware("http")
async def lan_only(request: Request, call_next):
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
        allowed = ip.is_private or ip.is_loopback or any(ip in n for n in _extra_networks())
    except ValueError:
        allowed = host in ("testclient", "localhost")
    if not allowed:
        return PlainTextResponse("Forbidden: LAN only", status_code=403)
    return await call_next(request)


def _check_id(run_id: str) -> str:
    if not RUN_ID.match(run_id) or not (runs_dir() / run_id / "meta.json").exists():
        raise HTTPException(404, "run not found")
    return run_id


def _headline(r: dict) -> dict:
    m = r.get("metrics") or {}
    return m.get("oos") or m if r["kind"] == "walkforward" else m


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__, "commit": env().ctlab_git_commit}


@app.get("/", response_class=HTMLResponse)
def index(kind: str | None = None, strategy: str | None = None) -> str:
    runs = list_runs() if runs_dir().exists() else []
    kinds = sorted({r["kind"] for r in runs})
    strategies = sorted({r.get("strategy", "") for r in runs})
    if kind:
        runs = [r for r in runs if r["kind"] == kind]
    if strategy:
        runs = [r for r in runs if r.get("strategy") == strategy]
    rows = [{"run": r, "m": _headline(r)} for r in runs]
    return jinja_env().get_template("index.html").render(
        rows=rows, kinds=kinds, strategies=strategies, kind=kind, strategy=strategy,
        max_compare=MAX_COMPARE)


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_report(run_id: str) -> str:
    _check_id(run_id)
    path = runs_dir() / run_id / "report.html"
    if not path.exists():
        build_report(run_id)
    return path.read_text()


@app.get("/runs/{run_id}/trades.csv")
def run_trades(run_id: str) -> Response:
    _check_id(run_id)
    p = runs_dir() / run_id / "trades.parquet"
    if not p.exists():
        raise HTTPException(404, "no trades")
    csv = pl.read_parquet(p).write_csv()
    return Response(csv, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{run_id}-trades.csv"'})


@app.get("/runs/{run_id}/meta.json")
def run_meta(run_id: str) -> JSONResponse:
    meta, metrics, _ = load_run(_check_id(run_id))
    return JSONResponse({"meta": meta, "metrics": metrics})


@app.get("/compare", response_class=HTMLResponse)
def compare(ids: list[str] = Query(default=[])) -> str:
    ids = [_check_id(i) for i in dict.fromkeys(ids)][:MAX_COMPARE]
    if len(ids) < 2:
        raise HTTPException(400, "select at least two runs")
    runs, curves = [], []
    for rid in ids:
        meta, metrics, frames = load_run(rid)
        m = metrics.get("oos") if meta["kind"] == "walkforward" else metrics
        params = meta.get("params") or meta.get("best_params") or meta.get("final_params") or {}
        runs.append({"id": rid, "meta": meta, "m": m or {}, "params": params,
                     "verdict": (metrics.get("verdict") or {}).get("label")})
        eq = frames.get("equity")
        init = (meta.get("engine") or {}).get("initial_balance", 10_000)
        if eq is not None and eq.height:
            eq = eq.sort("ts")
            curves.append((f"{meta.get('strategy')} · {meta['kind']} · {rid[:15]}",
                           list(zip(eq["ts"].to_list(),
                                    ((eq["equity"] / init - 1) * 100).to_list(), strict=True))))
    best = {}
    for key, _, mode in COMPARE_ROWS:
        vals = [r["m"].get(key) for r in runs if r["m"].get(key) is not None]
        if mode and len(vals) > 1:
            best[key] = max(vals) if mode == "max" else min(vals)
    param_names = sorted({k for r in runs for k in r["params"]})
    return jinja_env().get_template("compare.html").render(
        runs=runs, rows=COMPARE_ROWS, best=best, param_names=param_names,
        chart=line_chart(curves, height=300, y_suffix="%", title="return comparison"))
