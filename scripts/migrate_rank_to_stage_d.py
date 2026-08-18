"""Explicit one-time SQLite migration from historical Rank snapshots to Stage D."""

from __future__ import annotations

import argparse
import json

from app.config.settings import Settings
from app.storage.db import create_engine_from_url, migrate_rank_to_stage_d


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=None, help="Defaults to DATABASE_URL/.env")
    parser.add_argument("--apply", action="store_true", help="Apply the migration; without this flag only print the plan.")
    args = parser.parse_args()
    settings = Settings.from_env()
    engine = create_engine_from_url(args.database_url or settings.database_url)
    result = migrate_rank_to_stage_d(engine, apply=bool(args.apply))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
