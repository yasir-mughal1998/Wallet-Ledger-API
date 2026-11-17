"""Database-level guarantees that do not depend on the API code."""

import asyncio

import asyncpg
import pytest

from tests.conftest import DATABASE_URL


def execute(sql: str, *args):
    async def run():
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            return await conn.execute(sql, *args)
        finally:
            await conn.close()

    return asyncio.run(run())


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE ledger SET amount = amount + 1 WHERE user_id = $1",
        "DELETE FROM ledger WHERE user_id = $1",
    ],
)
def test_ledger_rows_cannot_be_changed(client, user_id, statement):
    client.post("/credit", json={"user_id": user_id, "amount": 100})
    with pytest.raises(asyncpg.PostgresError, match="immutable"):
        execute(statement, user_id)


def test_ledger_cannot_be_truncated():
    with pytest.raises(asyncpg.PostgresError, match="immutable"):
        execute("TRUNCATE ledger")


def test_database_refuses_a_negative_balance(client, user_id):
    # Second line of defence behind the debit statement's own balance check.
    client.post("/credit", json={"user_id": user_id, "amount": 100})
    with pytest.raises(asyncpg.CheckViolationError):
        execute("UPDATE accounts SET balance = balance - 101 WHERE user_id = $1", user_id)


def test_database_refuses_non_positive_amounts(user_id):
    with pytest.raises(asyncpg.CheckViolationError):
        execute("INSERT INTO ledger (user_id, type, amount) VALUES ($1, 'credit', 0)", user_id)
    with pytest.raises(asyncpg.CheckViolationError):
        execute("INSERT INTO ledger (user_id, type, amount) VALUES ($1, 'refund', 5)", user_id)
