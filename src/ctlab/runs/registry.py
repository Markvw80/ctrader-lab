"""Run registry: every run gets an id and a folder with everything needed to reproduce it.

results/runs/<run_id>/
  meta.json      strategy, params, symbol, timeframes, data range + fingerprint, settings,
                 cost model, code version (git commit, dirty flag, uv.lock hash), command
  metrics.json   summary metrics
  trades.parquet, equity.parquet, breakdown_<name>.parquet
"""

import dataclasses
import hashlib
import json
import secrets
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from ctlab import __version__
from ctlab.config import env

REPO_ROOT = Path(__file__).resolve().parents[3]


def runs_dir() -> Path:
    return env().ctlab_results_dir / "runs"


def new_run_id(label: str) -> str:
    return f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{label}-{secrets.token_hex(2)}"


def code_version() -> dict:
    commit = env().ctlab_git_commit
    if commit in ("", "unknown"):
        try:
            commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT,
                                    capture_output=True, text=True, check=True).stdout.strip()
            dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                                   cwd=REPO_ROOT, capture_output=True, text=True, check=False).stdout.strip()
            if dirty:
                commit += "-dirty"
        except (OSError, subprocess.CalledProcessError):
            commit = "unknown"
    lock = REPO_ROOT / "uv.lock"
    if not lock.exists():
        lock = Path("/app/uv.lock")
    lock_hash = hashlib.sha256(lock.read_bytes()).hexdigest()[:12] if lock.exists() else None
    return {"git_commit": commit, "ctlab_version": __version__, "uv_lock_sha256": lock_hash}


def data_fingerprint(bars: pl.DataFrame) -> dict:
    h = hashlib.sha256()
    h.update(bars["ts"].dt.epoch("ms").to_numpy().tobytes())
    h.update(bars["close"].to_numpy().tobytes())
    return {"rows": bars.height, "first": str(bars["ts"].min()), "last": str(bars["ts"].max()),
            "sha256": h.hexdigest()[:16]}


def save_run(run_id: str, kind: str, meta: dict, metrics: dict | None = None,
             frames: dict[str, pl.DataFrame] | None = None) -> Path:
    d = runs_dir() / run_id
    d.mkdir(parents=True, exist_ok=False)
    meta = {
        "run_id": run_id, "kind": kind,
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "command": " ".join(sys.argv),
        "code": code_version(),
        **meta,
    }
    (d / "meta.json").write_text(json.dumps(meta, indent=2, default=_json_default))
    if metrics is not None:
        (d / "metrics.json").write_text(json.dumps(metrics, indent=2, default=_json_default))
    for name, df in (frames or {}).items():
        df.write_parquet(d / f"{name}.parquet")
    return d


def list_runs() -> list[dict]:
    out = []
    for d in sorted(runs_dir().glob("*/meta.json"), reverse=True):
        meta = json.loads(d.read_text())
        mpath = d.parent / "metrics.json"
        meta["metrics"] = json.loads(mpath.read_text()) if mpath.exists() else {}
        out.append(meta)
    return out


def load_run(run_id: str) -> tuple[dict, dict, dict[str, pl.DataFrame]]:
    d = runs_dir() / run_id
    meta = json.loads((d / "meta.json").read_text())
    mpath = d / "metrics.json"
    metrics = json.loads(mpath.read_text()) if mpath.exists() else {}
    frames = {p.stem: pl.read_parquet(p) for p in d.glob("*.parquet")}
    return meta, metrics, frames


def _json_default(o):
    if dataclasses.is_dataclass(o):
        return dataclasses.asdict(o)
    return str(o)
