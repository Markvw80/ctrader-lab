"""HTML report per run: results/runs/<id>/report.html (self-contained, inline SVG charts)."""

import json
from pathlib import Path

import polars as pl
from jinja2 import Environment, FileSystemLoader, select_autoescape

from ctlab.config import symbol_spec
from ctlab.report.charts import bar_chart, line_chart, scatter
from ctlab.report.metrics import breakdowns_from_trades
from ctlab.runs.registry import load_run, runs_dir

TEMPLATES = Path(__file__).parent / "templates"
TRADES_SHOWN = 300


def jinja_env() -> Environment:
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))
    env.filters["num"] = fmt_num
    env.filters["ts"] = fmt_ts
    env.filters["date"] = lambda v: fmt_ts(v)[:10]
    env.filters["param"] = fmt_param
    env.filters["tojson_pretty"] = lambda v: json.dumps(v, indent=2, default=str)
    return env


def fmt_num(v, digits: int = 2) -> str:
    if v is None:
        return "–"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        return f"{v:,.{digits}f}"
    return str(v)


def fmt_param(v) -> str:
    return f"{v:.10g}" if isinstance(v, float) else str(v)


def fmt_ts(v) -> str:
    s = str(v)
    return s.replace("T", " ")[:16] if s and s != "None" else "–"


def equity_charts(equity: pl.DataFrame, initial: float) -> tuple[str, str]:
    if equity.height == 0:
        return '<p class="muted">no trades</p>', ""
    eq = equity.sort("ts").with_columns(peak=pl.col("equity").cum_max())
    eq = eq.with_columns(dd=(pl.col("equity") - pl.col("peak")) / pl.col("peak") * 100)
    pts = list(zip(eq["ts"].to_list(), eq["equity"].to_list(), strict=True))
    dd = list(zip(eq["ts"].to_list(), eq["dd"].to_list(), strict=True))
    return (line_chart([("equity", pts)], title="equity"),
            line_chart([("drawdown", dd)], height=150, y_suffix="%", fill_negative=True,
                       title="drawdown"))


def _sessions(meta: dict) -> dict:
    return meta.get("sessions") or symbol_spec(meta["symbol"], broker=False)["sessions"]


def _breakdown_section(trades: pl.DataFrame, sessions: dict) -> dict:
    b = breakdowns_from_trades(trades, sessions) if trades.height else {}
    charts = {}
    if b:
        charts["month"] = bar_chart(b["month"]["month"].to_list(), b["month"]["net"].to_list(),
                                    title="net per month")
        charts["weekday"] = bar_chart(b["weekday"]["weekday"].to_list(), b["weekday"]["net"].to_list(),
                                      height=160, title="net per weekday")
        charts["session"] = bar_chart(b["session"]["session"].to_list(), b["session"]["net"].to_list(),
                                      height=160, title="net per session")
    return {"tables": {k: v.to_dicts() for k, v in b.items()}, "charts": charts}


def _trades_table(trades: pl.DataFrame) -> list[dict]:
    cols = ["entry_ts", "exit_ts", "side", "lots", "entry_price", "exit_price", "exit_reason",
            "commission", "swap", "net_pnl", "r_multiple"]
    if "window" in trades.columns:
        cols = ["window", *cols]
    return trades.select([c for c in cols if c in trades.columns]).head(TRADES_SHOWN).to_dicts()


def _param_scatters(trials: pl.DataFrame, best: dict | None) -> list[dict]:
    if trials.height == 0 or "value" not in trials.columns:
        return []
    ok = trials.filter(~pl.col("rejected").fill_null(False)) if "rejected" in trials.columns else trials
    out = []
    for c in [c for c in trials.columns if c.startswith("p_")]:
        name = c[2:]
        col = ok[c]
        if col.dtype not in (pl.Float64, pl.Int64, pl.Float32, pl.Int32):
            continue
        hl = best.get(name) if best else None
        out.append({"name": name, "svg": scatter(col.to_list(), ok["value"].to_list(), name,
                                                  highlight=hl if isinstance(hl, (int, float)) else None)})
    return out


def build_report(run_id: str) -> Path:
    meta, metrics, frames = load_run(run_id)
    kind = meta["kind"]
    sessions = _sessions(meta)
    trades = frames.get("trades", pl.DataFrame())
    equity = frames.get("equity", pl.DataFrame({"ts": [], "equity": []}))
    initial = (meta.get("engine") or {}).get("initial_balance", 10_000)
    eq_svg, dd_svg = equity_charts(equity, initial)
    ctx = {
        "meta": meta, "metrics": metrics, "kind": kind, "run_id": run_id,
        "equity_svg": eq_svg, "drawdown_svg": dd_svg,
        "breakdown": _breakdown_section(trades, sessions),
        "trades": _trades_table(trades) if trades.height else [],
        "trades_total": trades.height, "trades_shown": TRADES_SHOWN,
        "headline": metrics.get("oos") if kind == "walkforward" else metrics,
    }
    if "sensitivity" in frames:
        ctx["sensitivity_rows"] = frames["sensitivity"].to_dicts()
    if "trials" in frames:
        t = frames["trials"]
        if "window" in t.columns and t.height:
            t = t.filter(pl.col("window") == t["window"].max())
        best = meta.get("best_params") or meta.get("final_params")
        ctx["scatters"] = _param_scatters(t, best)
        ctx["top_trials"] = (t.sort("value", descending=True, nulls_last=True).head(15).to_dicts()
                             if t.height else [])
    if kind == "walkforward" and "windows" in frames:
        w = frames["windows"]
        ctx["windows"] = [{**r, "params": json.loads(r["params"]) if r["params"] else None}
                          for r in w.to_dicts()]
        ctx["windows_svg"] = bar_chart([f"W{r['window']}" for r in ctx["windows"]],
                                       [r["oos_net"] or 0.0 for r in ctx["windows"]], height=160,
                                       title="out-of-sample net per window")
    html = jinja_env().get_template("report.html").render(**ctx)
    path = runs_dir() / run_id / "report.html"
    path.write_text(html)
    return path
