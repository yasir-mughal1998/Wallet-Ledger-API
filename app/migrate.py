"""Apply SQL migrations from ./migrations, once each, in file-name order.

Run by the container entrypoint before the API starts:  python -m app.migrate
"""

import asyncio
import os
import pathlib
import sys

import asyncpg

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "migrations"
# Arbitrary constant: the advisory lock makes concurrent starters (several API
# instances booting together) apply migrations one at a time.
ADVISORY_LOCK_ID = 727_001


async def _connect(dsn: str, attempts: int = 60) -> asyncpg.Connection:
    for attempt in range(attempts):
        try:
            return await asyncpg.connect(dsn)
        except (OSError, asyncpg.PostgresError):
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(1)  # database still starting up
    raise AssertionError("unreachable")


async def migrate(dsn: str) -> None:
    conn = await _connect(dsn)
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", ADVISORY_LOCK_ID)
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        applied = {row["name"] for row in await conn.fetch("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            # Schema change and its bookkeeping row commit together.
            async with conn.transaction():
                await conn.execute(path.read_text())
                await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", path.name)
            print(f"migrate: applied {path.name}", flush=True)
    finally:
        await conn.close()  # closing the session also releases the advisory lock


if __name__ == "__main__":
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        sys.exit("DATABASE_URL environment variable must be set")
    asyncio.run(migrate(database_url))
