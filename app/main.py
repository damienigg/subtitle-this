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


# Subtitle creation is exclusively a manual user action through the web UI,
# and only ever per-item or per-batch — never library-wide. We deliberately
# do NOT expose:
# - a webhook receiver (no auto-triggering on item-added events)
# - a path-based /transcribe-translate endpoint (no curl-driven workflow)
# - a sweep-everything endpoint (no whole-library subtitling — too much
#   spend potential, and there's no real use case that "subtitle every
#   item in my 5000-film library" addresses better than a deliberate
#   batch selection)
# The endpoints registered below back the per-item "Subtitle this" button
# and the multi-select batch flow on the Library page, plus the auto-
# refreshing jobs list — they're not meant as a public CLI surface.
app = FastAPI(title="Subtitle This", version="0.4.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


app.include_router(manage_router)
app.include_router(settings_router)
app.include_router(ui_router)
