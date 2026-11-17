"""Seed the ledger with 10M+ rows across 100k+ users, unevenly spread.

    docker compose run --rm seed                # 10,076,000 rows, 100,000 users
    docker compose run --rm seed --scale 0.1    #  1,007,600 rows,  10,000 users

Rows are generated inside PostgreSQL (generate_series), so no data crosses the
network. Users are loaded in chunks over a few parallel connections. Each chunk
is ONE statement that inserts the ledger rows and, from those same rows, the
matching account balances - so a chunk is all-or-nothing and the accounts table
agrees with the ledger even if the seed is interrupted.

Per user, row n (1, 2, 3, ...) is a debit when n is a multiple of 3 and a
credit otherwise. Credits are 1,000..100,000 and debits 1..1,000, and every
debit comes after two credits, so no balance is ever below zero at any point.
"""

import argparse
import asyncio
import os
import sys
import time

import asyncpg

# (number of users, ledger rows per user) at --scale 1.0
TIERS = [
    (20, 150_000),   # heavy users:    3,000,000 rows
    (1_980, 1_000),  # regular users:  1,980,000 rows
    (98_000, 52),    # long tail:      5,096,000 rows
]
ROWS_PER_CHUNK = 250_000
USER_ID_FORMAT = "seed_{:07d}"  # seed_0000001, seed_0000002, ...

# $1..$2 = user number range, $3 = rows per user, $4 = timestamp of the newest row
CHUNK_SQL = """
WITH new_rows AS (
    INSERT INTO ledger (user_id, type, amount, created_at)
    SELECT 'seed_' || lpad(u::text, 7, '0'),
           CASE WHEN n % 3 = 0 THEN 'debit' ELSE 'credit' END,
           CASE WHEN n % 3 = 0 THEN 1 + (u * 31 + n * 17) % 1000
                ELSE 1000 + (u * 7919 + n * 104729) % 99001 END,
           $4::timestamptz - make_interval(secs => ($3::bigint - n) * 30)
    FROM generate_series($1::bigint, $2::bigint) AS u
    CROSS JOIN generate_series(1::bigint, $3::bigint) AS n
    ORDER BY u, n
    RETURNING user_id, type, amount
)
INSERT INTO accounts (user_id, balance)
SELECT user_id, SUM(CASE WHEN type = 'credit' THEN amount ELSE -amount END)
FROM new_rows
GROUP BY user_id
"""

# Same check the reviewers will run: accounts vs. the ledger, both directions.
MISMATCH_SQL = """
SELECT count(*)
FROM accounts a
FULL JOIN (
    SELECT user_id, SUM(CASE WHEN type = 'credit' THEN amount ELSE -amount END) AS balance
    FROM ledger GROUP BY user_id
) l USING (user_id)
WHERE a.balance IS DISTINCT FROM l.balance
"""


def plan_chunks(scale: float) -> list[tuple[int, int, int]]:
    """Split the user number space into (first_user, last_user, rows_per_user)."""
    chunks = []
    next_user = 1
    for users, rows_per_user in TIERS:
        users = max(1, round(users * scale))
        users_per_chunk = max(1, ROWS_PER_CHUNK // rows_per_user)
        tier_end = next_user + users - 1
        while next_user <= tier_end:
            last = min(next_user + users_per_chunk - 1, tier_end)
            chunks.append((next_user, last, rows_per_user))
            next_user = last + 1
    return chunks


async def _worker(dsn: str, queue: asyncio.Queue, newest: object, progress: dict) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        # Seed-only speed-ups for this session. If the database crashed mid-seed
        # we would simply reseed, so waiting for each WAL flush buys nothing.
        await conn.execute("SET synchronous_commit = off")
        await conn.execute("SET work_mem = '256MB'")
        while True:
            try:
                first, last, rows_per_user = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            await conn.execute(CHUNK_SQL, first, last, rows_per_user, newest)
            progress["rows"] += (last - first + 1) * rows_per_user
            print(f"seed: {progress['rows']:>12,} / {progress['total']:,} rows", flush=True)
    finally:
        await conn.close()


async def seed(dsn: str, scale: float, workers: int) -> None:
    chunks = plan_chunks(scale)
    total_rows = sum((last - first + 1) * rows for first, last, rows in chunks)
    total_users = chunks[-1][1]

    conn = await asyncpg.connect(dsn)
    try:
        if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM accounts WHERE user_id LIKE 'seed\\_%')"):
            sys.exit(
                "seed: seed users already exist. To start again from an empty "
                "database run: docker compose down -v && docker compose up -d"
            )
        newest = await conn.fetchval("SELECT now()")

        print(f"seed: loading {total_rows:,} ledger rows for {total_users:,} users", flush=True)
        started = time.monotonic()

        queue: asyncio.Queue = asyncio.Queue()
        for chunk in chunks:
            queue.put_nowait(chunk)
        progress = {"rows": 0, "total": total_rows}
        await asyncio.gather(*(_worker(dsn, queue, newest, progress) for _ in range(workers)))
        loaded = time.monotonic()
        print(f"seed: rows loaded in {loaded - started:.1f}s; running VACUUM ANALYZE", flush=True)

        # Refresh planner statistics and the visibility map after the bulk load.
        await conn.execute("VACUUM (ANALYZE) accounts", timeout=None)
        await conn.execute("VACUUM (ANALYZE) ledger", timeout=None)
        vacuumed = time.monotonic()

        mismatches = await conn.fetchval(MISMATCH_SQL, timeout=None)
        ledger_rows = await conn.fetchval("SELECT count(*) FROM ledger", timeout=None)
        accounts = await conn.fetchval("SELECT count(*) FROM accounts")
        biggest = await conn.fetchval(
            "SELECT count(*) FROM ledger WHERE user_id = $1", USER_ID_FORMAT.format(1)
        )
        finished = time.monotonic()
    finally:
        await conn.close()

    print(f"seed: ledger rows in table   : {ledger_rows:,}")
    print(f"seed: accounts in table      : {accounts:,}")
    print(f"seed: rows for {USER_ID_FORMAT.format(1)}  : {biggest:,}")
    print(f"seed: balance/ledger mismatches: {mismatches}")
    print(
        f"seed: load {loaded - started:.1f}s + vacuum/analyze {vacuumed - loaded:.1f}s"
        f" + verification {finished - vacuumed:.1f}s = {finished - started:.1f}s total"
    )
    if mismatches:
        sys.exit("seed: FAILED - accounts do not match the ledger")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--scale", type=float, default=1.0, help="1.0 = 10M rows / 100k users (default)")
    parser.add_argument("--workers", type=int, default=4, help="parallel database connections")
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL environment variable must be set")
    asyncio.run(seed(dsn, args.scale, args.workers))


if __name__ == "__main__":
    main()
