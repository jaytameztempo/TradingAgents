"""The FastAPI app: JSON API under /api, and the built React app at /."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ui.server import paths
from ui.server.routes import broker, jobs, pipeline, queues, research
from ui.server.routes import settings as settings_routes
from ui.server.scheduler import scheduler

# The same .env the scripts read, so TRADINGAGENTS_RESULTS_DIR and the paper keys are visible here.
load_dotenv(paths.REPO_ROOT / ".env")



@asynccontextmanager
async def lifespan(_app: FastAPI):
    # The scheduler runs while the server runs; BATS_SCHEDULER=0 turns it off.
    if os.environ.get("BATS_SCHEDULER", "1") != "0":
        scheduler.start_thread()
    yield
    scheduler.stop_thread()


app = FastAPI(title="BATS local UI", docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json",
              lifespan=lifespan)
for module in (pipeline, research, jobs, broker, settings_routes, queues):
    app.include_router(module.router)


@app.get("/api/health")
def health() -> dict:
    status = scheduler.status()
    # orders_enabled: only the scheduler's paper auto-send can send orders; no route can.
    return {"ok": True, "data_home": str(paths.data_home()), "orders_enabled": status["auto_send_enabled"],
            "scheduler_running": status["running"]}


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
