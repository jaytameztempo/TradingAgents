"""The FastAPI app: JSON API under /api, and the built React app at /."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from extensions.tradebot import store
from ui.server import paths
from ui.server.routes import broker, jobs, orders, pipeline, queues, research, risk, trade, tradebot
from ui.server.routes import settings as settings_routes
from ui.server.scheduler import scheduler

# The same .env the scripts read, so TRADINGAGENTS_RESULTS_DIR and the paper keys are visible here.
load_dotenv(paths.REPO_ROOT / ".env")



def scheduler_refusal() -> str | None:
    """Why the scheduler must not start: the data folder is inside a OneDrive-synced folder, where sync can lock
    or roll back the queue, ledger and lock files. None when it may start."""
    where = store.synced_folder()
    if where:
        return (f"the data folder {store.data_home()} is inside {where}; the scheduler is not started. Move it "
                f"out of OneDrive (or set {store.HOME_ENV}) and restart the server")
    return None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # The scheduler runs while the server runs; BATS_SCHEDULER=0 turns it off, and a synced data folder refuses it.
    if os.environ.get("BATS_SCHEDULER", "1") != "0" and scheduler_refusal() is None:
        scheduler.start_thread()
    yield
    scheduler.stop_thread()


app = FastAPI(title="BATS local UI", docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json",
              lifespan=lifespan)
for module in (pipeline, research, jobs, broker, settings_routes, queues, trade, risk, orders, tradebot):
    app.include_router(module.router)


@app.get("/api/health")
def health() -> dict:
    status = scheduler.status()
    # orders_enabled: the scheduler sends on its own (tradebot auto mode and paper auto-send both on). Apart from
    # it, only a confirmed send on the Trade page starts the paper sender; no route talks to the broker itself.
    return {"ok": True, "data_home": str(paths.data_home()), "orders_enabled": status["auto_send_sends"],
            "scheduler_running": status["running"], "scheduler_refused": scheduler_refusal()}


if paths.WEB_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=paths.WEB_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        """Serve the React app; client-side routes all load index.html. Unknown /api paths are a 404."""
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        candidate = (paths.WEB_DIST / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(paths.WEB_DIST.resolve()):
            return FileResponse(candidate)
        return FileResponse(paths.WEB_DIST / "index.html")
