"""LAN-only web page for comparing runs. Full UI in phase 5."""

from fastapi import FastAPI

from ctlab import __version__
from ctlab.config import env

app = FastAPI(title="ctrader-lab", docs_url=None, redoc_url=None)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__, "commit": env().ctlab_git_commit}


@app.get("/")
def index() -> dict:
    return {"message": "ctrader-lab is running. Run comparison UI arrives in phase 5."}
