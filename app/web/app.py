from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.web.routes import all_items, api, dynamics, events, github, home, search, sources


def create_app(
    *,
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
    init_database: bool = True,
    github_data_path: str | Path | None = None,
    intel_output_root: str | Path = "output",
) -> FastAPI:
    app = FastAPI(title="AI 热点内容台")

    if settings is None:
        settings = Settings.from_env()

    if session_factory is None:
        engine = create_engine_from_url(settings.database_url)
        if init_database:
            init_db(engine)
        session_factory = create_session_factory(engine)

    app.state.session_factory = session_factory
    app.state.github_data_path = github_data_path
    app.state.intel_output_root = intel_output_root
    app.state.topic_categories = settings.ai_review_categories

    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    app.include_router(home.router)
    app.include_router(all_items.router)
    app.include_router(dynamics.router)
    app.include_router(events.router)
    app.include_router(github.router)
    app.include_router(search.router)
    app.include_router(sources.router)
    app.include_router(api.router)
    return app


app = create_app()
