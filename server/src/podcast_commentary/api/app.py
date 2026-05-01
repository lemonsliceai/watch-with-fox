import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from podcast_commentary.api.routes import personas, sessions
from podcast_commentary.core.db import ensure_schema, warm_pool

logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await warm_pool()
    await ensure_schema()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Podcast Commentary API", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(sessions.router)
    app.include_router(personas.router)

    # Serve persona avatar images to LemonSlice Cloud via AVATAR_BASE_URL.
    # fox_2x3.jpg is retained until the in-review Chrome Web Store build
    # (which still references the Fox persona) is approved or superseded.
    static_dir = Path(__file__).parent.parent / "core" / "data"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app


app = create_app()
