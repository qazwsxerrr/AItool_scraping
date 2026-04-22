from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config.settings import Settings
from app.storage.db import create_engine_from_url, init_db


def main() -> None:
    settings = Settings.from_env()
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    print(f"Initialized database: {settings.database_url}")


if __name__ == "__main__":
    main()
