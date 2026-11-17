"""Shared helpers. API tests are black-box: they call a running API over HTTP
(BASE_URL) and check the database directly (DATABASE_URL), the way a reviewer would.
"""

import asyncio
import os
import uuid

import asyncpg
import httpx
import pytest

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8080")
API_TOKEN = os.environ.get("API_TOKEN", "recruitment-test-token")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://wallet:wallet@localhost:5432/wallet")

LEDGER_BALANCE_SQL = (
    "SELECT COALESCE(SUM(CASE WHEN type = 'credit' THEN amount ELSE -amount END), 0)::bigint"
    " FROM ledger WHERE user_id = $1"
)


@pytest.fixture(scope="session")
def client():
    """Authenticated HTTP client. httpx.Client is safe to share between threads."""
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=200)
    with httpx.Client(
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {API_TOKEN}"},
        limits=limits,
        timeout=60,
    ) as http:
        yield http


@pytest.fixture(scope="session")
def anonymous():
    """HTTP client that sends no Authorization header."""
    with httpx.Client(base_url=BASE_URL, timeout=30) as http:
        yield http


@pytest.fixture
def user_id() -> str:
    """A user id no other test (or earlier run) has used."""
    return "t_" + uuid.uuid4().hex


def db_fetchval(sql: str, *args):
    async def run():
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            return await conn.fetchval(sql, *args)
        finally:
            await conn.close()

    return asyncio.run(run())


def ledger_balance(user: str) -> int:
    """The balance according to the source of truth: credits minus debits."""
    return db_fetchval(LEDGER_BALANCE_SQL, user)


def ledger_rows(user: str) -> int:
    return db_fetchval("SELECT count(*) FROM ledger WHERE user_id = $1", user)
