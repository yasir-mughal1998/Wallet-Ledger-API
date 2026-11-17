"""Ledger operations: the only module that talks to the database for requests.

Consistency model, in short:

* Each credit/debit is ONE SQL statement that changes the account row and
  appends the ledger row. One statement is one transaction, so both happen or
  neither does, and it costs a single network round trip.
* The account row's lock serialises writers of the same user. Under READ
  COMMITTED, a writer that waited for the lock re-reads the freshly committed
  row before applying its own change, so no update is lost and the
  "balance >= amount" test of a debit is always made against the latest balance.
* Every transaction locks exactly one account row, so writers of different
  users never wait for each other and lock-order deadlocks cannot occur.
"""

import asyncio
import random

import asyncpg

from app.errors import (
    insufficient_funds,
    invalid_request,
    service_unavailable,
    user_not_found,
)
from app.locks import KeyedLock

# Upsert the account (creating the user on first credit), append the ledger
# row, and return the balance right after this operation.
CREDIT_SQL = """
WITH acct AS (
    INSERT INTO accounts AS a (user_id, balance)
    VALUES ($1::text, $2::bigint)
    ON CONFLICT (user_id) DO UPDATE SET balance = a.balance + EXCLUDED.balance
    RETURNING balance
), entry AS (
    INSERT INTO ledger (user_id, type, amount)
    SELECT $1::text, 'credit', $2::bigint FROM acct
)
SELECT balance FROM acct
"""

# Subtract only if the balance covers the amount. The ledger row is inserted
# only if the UPDATE changed a row, so a rejected debit writes nothing.
# user_exists tells "unknown user" (404) apart from "not enough money" (409).
DEBIT_SQL = """
WITH acct AS (
    UPDATE accounts
    SET balance = balance - $2::bigint
    WHERE user_id = $1::text AND balance >= $2::bigint
    RETURNING balance
), entry AS (
    INSERT INTO ledger (user_id, type, amount)
    SELECT $1::text, 'debit', $2::bigint FROM acct
)
SELECT (SELECT balance FROM acct) AS balance,
       EXISTS (SELECT 1 FROM accounts WHERE user_id = $1::text) AS user_exists
"""

# Primary-key lookup: cost does not depend on ledger size or user history.
BALANCE_SQL = "SELECT balance FROM accounts WHERE user_id = $1::text"

# Returns the key if we claimed it, nothing if it already exists. If another
# transaction is inserting the same key right now, this waits for it to finish.
CLAIM_KEY_SQL = """
INSERT INTO idempotency_keys (key, type, user_id, amount)
VALUES ($1::text, $2::text, $3::text, $4::bigint)
ON CONFLICT (key) DO NOTHING
RETURNING key
"""
READ_KEY_SQL = "SELECT type, user_id, amount, balance FROM idempotency_keys WHERE key = $1::text"
STORE_KEY_RESULT_SQL = "UPDATE idempotency_keys SET balance = $2::bigint WHERE key = $1::text"

MAX_ATTEMPTS = 5


class _Retry(Exception):
    """Internal: the attempt wrote nothing and should simply be run again."""


# Errors after which PostgreSQL guarantees the transaction was rolled back, so
# running it again is safe. With one row lock per transaction they are not
# expected, but they must never reach the client as a 500.
_RETRYABLE = (asyncpg.DeadlockDetectedError, asyncpg.SerializationError, _Retry)


class Ledger:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._user_locks = KeyedLock()

    async def balance(self, user_id: str) -> int:
        # Plain read: under MVCC it never waits for writers and never blocks them.
        balance = await self._pool.fetchval(BALANCE_SQL, user_id)
        if balance is None:
            raise user_not_found()
        return balance

    async def credit(self, user_id: str, amount: int, idempotency_key: str | None = None) -> int:
        return await self._write("credit", user_id, amount, idempotency_key)

    async def debit(self, user_id: str, amount: int, idempotency_key: str | None = None) -> int:
        return await self._write("debit", user_id, amount, idempotency_key)

    async def _write(self, op: str, user_id: str, amount: int, key: str | None) -> int:
        # Queue same-user writes in memory so that a hot user uses at most one
        # pooled connection per process (see KeyedLock for why).
        async with self._user_locks.hold(user_id):
            for attempt in range(MAX_ATTEMPTS):
                try:
                    async with self._pool.acquire() as conn:
                        if key is None:
                            return await _apply(conn, op, user_id, amount)
                        return await _apply_idempotent(conn, op, user_id, amount, key)
                except _RETRYABLE:
                    # Short, jittered, growing pause before the next attempt.
                    await asyncio.sleep(random.uniform(0.001, 0.005) * (attempt + 1))
        raise service_unavailable("could not complete the operation, please retry")


async def _apply(conn: asyncpg.Connection, op: str, user_id: str, amount: int) -> int:
    """Run one credit or debit statement; return the balance after it."""
    if op == "credit":
        return await conn.fetchval(CREDIT_SQL, user_id, amount)

    row = await conn.fetchrow(DEBIT_SQL, user_id, amount)
    if row["balance"] is not None:
        return row["balance"]
    if row["user_exists"]:
        raise insufficient_funds()
    raise user_not_found()


async def _apply_idempotent(
    conn: asyncpg.Connection, op: str, user_id: str, amount: int, key: str
) -> int:
    """Apply the operation at most once per Idempotency-Key.

    The key is claimed first, in the same transaction as the ledger write:
    * a concurrent duplicate blocks on the key's unique index until we commit,
      then sees our stored result and replays it;
    * if the operation is rejected (404/409) the exception rolls the whole
      transaction back, claim included, so only applied operations keep a key.
    """
    async with conn.transaction():
        claimed = await conn.fetchval(CLAIM_KEY_SQL, key, op, user_id, amount)
        if claimed is None:
            prior = await conn.fetchrow(READ_KEY_SQL, key)
            if prior is None:
                # The key existed a moment ago but is gone (e.g. pruned): start over.
                raise _Retry()
            if (prior["type"], prior["user_id"], prior["amount"]) != (op, user_id, amount):
                raise invalid_request("Idempotency-Key was already used with a different request")
            return prior["balance"]

        balance = await _apply(conn, op, user_id, amount)
        await conn.execute(STORE_KEY_RESULT_SQL, key, balance)
        return balance

