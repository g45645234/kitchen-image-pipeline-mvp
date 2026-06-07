from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.db import async_session_maker
from app.services.storage_service import cleanup_storage


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect or delete orphan files in local storage")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Report orphan and missing files without deleting anything")
    mode.add_argument("--delete", action="store_true", help="Delete orphan files reported by the cleanup scan")
    return parser.parse_args(argv)


async def run(dry_run: bool) -> dict:
    async with async_session_maker() as db:
        return await cleanup_storage(dry_run=dry_run, db=db)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    dry_run = not args.delete
    result = asyncio.run(run(dry_run=dry_run))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
