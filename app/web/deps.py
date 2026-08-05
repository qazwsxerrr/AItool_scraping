from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Iterator

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.github_reader import GitHubProjectReader
from app.storage.read_repository import UIReadRepository


TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def format_datetime(value: datetime | None) -> str:
    if value is None:
        return "未知时间"
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


def relative_time(value: datetime | None) -> str:
    if value is None:
        return "未知"
    now = datetime.now(timezone.utc)
    aware_value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    delta = now - aware_value.astimezone(timezone.utc)
    if delta.days > 0:
        return f"{delta.days}天前"
    hours = delta.seconds // 3600
    if hours > 0:
        return f"{hours}小时前"
    minutes = delta.seconds // 60
    if minutes > 0:
        return f"{minutes}分钟前"
    return "刚刚"


templates.env.filters["datetime"] = format_datetime
templates.env.filters["relative_time"] = relative_time


@lru_cache(maxsize=1)
def get_default_session_factory() -> sessionmaker[Session]:
    settings = Settings.from_env()
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    return create_session_factory(engine)


def get_repository(request: Request) -> Iterator[UIReadRepository]:
    session_factory: sessionmaker[Session] = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        session_factory = get_default_session_factory()
    with session_factory() as session:
        yield UIReadRepository(session)


def get_github_project_reader(request: Request) -> GitHubProjectReader:
    data_path = getattr(request.app.state, "github_data_path", None)
    output_root = getattr(request.app.state, "intel_output_root", "output")
    return GitHubProjectReader(data_path=data_path, output_root=output_root)
