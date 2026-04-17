import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from podcast_commentary.api.routes import sessions
from podcast_commentary.core.db import run_migrations, warm_pool

logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await warm_pool()
    await run_migrations()
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

    # Serve fox_2x3.jpg and other static files
    static_dir = Path(__file__).parent.parent / "core" / "data"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app


app = create_app()
