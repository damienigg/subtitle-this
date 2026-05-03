import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import jobs
from app.api.manage import router as manage_router
from app.api.settings_api import router as settings_router
from app.ui.routes import router as ui_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Capture the main event loop so sync routes can schedule async jobs.
    jobs.set_main_loop(asyncio.get_running_loop())
    yield


# Subtitle creation is exclusively a manual user action through the web UI.
# We deliberately do NOT expose:
# - an Emby webhook receiver (no auto-triggering on item-added events)
# - a path-based /transcribe-translate endpoint (no curl-driven workflow)
# The endpoints registered below back the UI buttons (library "Subtitle this",
# dashboard "Sweep") and the auto-refreshing jobs list — they're not meant as
# a public CLI surface.
app = FastAPI(title="Subtitle This", version="0.3.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


app.include_router(manage_router)
app.include_router(settings_router)
app.include_router(ui_router)
