# Wallet Ledger API

An HTTP API that keeps a money ledger for many users, with three operations:
`credit`, `debit` and `get_balance`. Python 3.13 + FastAPI + asyncpg, backed by
PostgreSQL 17.

## Run it

```bash
docker compose up            # database + API on http://localhost:8080
```

That is the only step. The API container applies the schema migrations itself
before it starts listening. The bearer token comes from the `API_TOKEN`
variable and defaults to `recruitment-test-token`; override it with
`API_TOKEN=something docker compose up`.

```bash
TOKEN=recruitment-test-token
curl -X POST localhost:8080/credit -H "Authorization: Bearer $TOKEN" -d '{"user_id":"u123","amount":10000}'
curl -X POST localhost:8080/debit  -H "Authorization: Bearer $TOKEN" -d '{"user_id":"u123","amount":4000}'
curl localhost:8080/balance/u123   -H "Authorization: Bearer $TOKEN"
```

| Task | Command |
|---|---|
| Load the seed (10,076,000 rows, 100,000 users) | `docker compose run --rm seed` |
| Load a 1M-row seed instead | `docker compose run --rm seed --scale 0.1` |
| Run the automated tests | `docker compose run --rm tests` |
| Run the load test | `docker compose run --rm loadtest` |
| Reconcile balances against the ledger | `docker compose exec -T db psql -U wallet -d wallet < scripts/reconcile.sql` |
| Open a SQL shell | `docker compose exec db psql -U wallet -d wallet` |
| Start again from an empty database | `docker compose down -v && docker compose up` |

The database port is not published to the host, so `docker compose up` cannot
fail because something else already uses 5432. Use the `psql` command above.

## API

All bodies are JSON. Amounts and balances are integers in minor units.

| Endpoint | Body | Success |
|---|---|---|
| `POST /credit` | `{"user_id": "u123", "amount": 10000}` | `200 {"user_id": "u123", "balance": 25000}` |
| `POST /debit` | `{"user_id": "u123", "amount": 4000}` | `200 {"user_id": "u123", "balance": 21000}` |
| `GET /balance/{user_id}` | none | `200 {"user_id": "u123", "balance": 21000}` |
| `GET /health` (extra) | none | `200 {"status": "ok"}` |

Errors are `{"error": "<code>", "message": "<text>"}`:

| Status | Code | When |
|---|---|---|
| 400 | `invalid_request` | Malformed JSON, missing field, a field breaking the input rules, or a bad/reused `Idempotency-Key` |
| 401 | `unauthorized` | Missing or wrong bearer token (checked before anything else, on every path) |
| 404 | `user_not_found` | `get_balance` or `debit` for a user that was never credited |
| 404 | `not_found` | Unknown path |
| 405 | `method_not_allowed` | Known path, wrong method |
| 409 | `insufficient_funds` | Debit larger than the balance; nothing is written |
| 503 | `service_unavailable` | Database unreachable, or a write still conflicting after 5 retries |

Details worth knowing:

- `amount` must be a JSON integer from 1 to 1,000,000,000. `10.0`, `1e3`,
  `"100"`, `true`, `0` and negatives are all rejected.
- `user_id` is 1 to 64 characters of `A-Z a-z 0-9 _ -`.
- Unknown fields are ignored. The `Content-Type` header is not checked.
- The `Bearer` scheme name is matched case-insensitively (RFC 7235); the token
  is compared exactly, in constant time.

### Optional extras

- **`Idempotency-Key`** header on `POST /credit` and `POST /debit` (1 to 255
  printable ASCII characters). A repeat of the same request with the same key
  is applied once and returns the original response. The same key with a
  different endpoint, user or amount returns `400`. Only applied operations
  keep their key: a request that was rejected (404/409) can be retried with the
  same key and is evaluated again.
- **`GET /health`** runs `SELECT 1`. It requires the token like every endpoint.
- **Graceful shutdown**: on `SIGTERM` uvicorn stops accepting connections,
  finishes in-flight requests (up to 20 s), then the connection pool is closed.

## Project layout

```
app/main.py         routes, error handlers, startup/shutdown
app/auth.py         bearer-token middleware
app/validation.py   input rules
app/ledger.py       the SQL and the retry / idempotency logic
app/locks.py        per-user in-process lock
app/migrate.py      migration runner
migrations/         schema and indexes
scripts/seed.py     seed script
scripts/loadtest.py load test
scripts/reconcile.sql
tests/              automated tests (API contract, concurrency, idempotency, schema, units)
results/            load-test output quoted below
```

## Design

### Schema

Full definition: [migrations/001_init.sql](migrations/001_init.sql).

```sql
CREATE TABLE accounts (
    user_id text COLLATE "C" PRIMARY KEY,
    balance bigint NOT NULL CHECK (balance >= 0)
) WITH (fillfactor = 80);

CREATE TABLE ledger (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    text COLLATE "C" NOT NULL,
    type       text NOT NULL CHECK (type IN ('credit', 'debit')),
    amount     bigint NOT NULL CHECK (amount > 0),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ledger_user_id_idx ON ledger (user_id);
```

- **`ledger`** is the source of truth: one row per credit or debit, never
  changed. Triggers reject `UPDATE`, `DELETE` and `TRUNCATE` on it, so
  immutability is enforced by the database, not only by the application.
- **`accounts`** holds one row per user with the current balance. It is what
  makes `get_balance` fast: a primary-key lookup whose cost does not depend on
  how many ledger rows exist or how long the user's history is.
- **`idempotency_keys`** backs the optional `Idempotency-Key` header.

Indexes, and why each exists:

| Index | Used for |
|---|---|
| `accounts_pkey (user_id)` | Every request: find the user's balance row |
| `ledger_pkey (id)` | Row identity |
| `ledger_user_id_idx (user_id)` | Not used by the API. It lets one user's history be audited or re-summed without scanning all rows |
| `idempotency_keys_pkey (key)` | Finding and locking a key |

Sizes with the 10M seed: ledger 1,041 MB in total (of which `ledger_pkey`
216 MB and `ledger_user_id_idx` 90 MB), accounts 10 MB.

### How a write works

Each credit or debit is **one SQL statement** that changes the balance and
appends the ledger row:

```sql
-- debit
WITH acct AS (
    UPDATE accounts SET balance = balance - $2
    WHERE user_id = $1 AND balance >= $2
    RETURNING balance
), entry AS (
    INSERT INTO ledger (user_id, type, amount)
    SELECT $1, 'debit', $2 FROM acct
)
SELECT (SELECT balance FROM acct) AS balance,
       EXISTS (SELECT 1 FROM accounts WHERE user_id = $1) AS user_exists;
```

Credit is the same shape with `INSERT ... ON CONFLICT (user_id) DO UPDATE SET
balance = balance + amount`, which also creates the user on first credit.

What this gives:

- **Atomicity.** One statement is one transaction. The balance change and the
  ledger row are committed together or not at all, including on a crash.
- **Balance always equals the ledger.** There is no code path that changes one
  without the other.
- **No lost updates, no negative balance.** The `UPDATE` takes the account
  row's lock. A second writer for the same user waits for it; when it gets the
  lock, PostgreSQL (at READ COMMITTED) re-reads the newly committed row and
  re-checks `balance >= $2` against it before applying its own change. So two
  debits of 60 against 100 give exactly one success: the second re-checks
  against 40 and updates nothing, which also inserts no ledger row.
- **A rejected debit writes nothing.** The ledger insert selects from the
  `UPDATE`'s result; no updated row means no inserted row.
- **One network round trip** per request, and no multi-statement transaction
  holding a lock while the application does something else.
- **Read-your-writes.** A 200 is sent only after the commit is flushed to disk
  (`synchronous_commit` is left on), and reads go to the same primary.
- **Isolation across users.** Each transaction locks exactly one account row.
  Writers for different users never wait on each other, and because no
  transaction ever holds two row locks, lock-order deadlocks cannot happen.
- **Second line of defence.** `CHECK (balance >= 0)` would abort any statement
  that tried to store a negative balance.

**Transaction isolation level: READ COMMITTED** (the PostgreSQL default). It is
sufficient here because every decision is made inside a single statement on a
row that statement has locked. Higher levels would add serialization failures
to retry and nothing else.

**Retries.** Deadlock (`40P01`) and serialization (`40001`) errors are not
expected with this design, but if one occurs the operation is retried up to 5
times with a short jittered pause. PostgreSQL guarantees the failed transaction
was rolled back, so a retry cannot double-apply. If all retries fail the client
gets `503`, never a `500` or a wrong balance.

### Hot users and the connection pool

The row lock keeps a hot user correct, but on its own it has a side effect: 100
concurrent writes to one user would each take a pooled database connection and
then sit waiting for the same row lock. The pool would be full of waiters and
requests for every other user would queue behind them.

So writes for the same user are first queued **in memory, per process**
([app/locks.py](app/locks.py)). A hot user then uses at most one connection per
API process (4 processes by default, 12 connections each), and the rest of the
pool stays free for other users. This lock is only about fairness. Correctness
never depends on it; the database row lock still does that job across
processes and instances.

Balance reads take no locks at all (MVCC), so they are never blocked by writes.

### Authentication

A plain ASGI middleware wraps the whole application
([app/auth.py](app/auth.py)). It runs before routing, body parsing and any
database access, and covers unknown paths too. The token is read from
`API_TOKEN` at startup (the process refuses to start without it), compared with
`hmac.compare_digest` on SHA-256 digests (constant time, and the token's length
is not revealed), and is never logged or put in a response. Access logging is
off.

### Idempotency keys

Inside one transaction: claim the key with `INSERT ... ON CONFLICT DO NOTHING`,
apply the operation, store the resulting balance on the key row, commit. A
concurrent duplicate blocks on the key's unique index until the first request
commits, then finds the stored result and returns it. If the operation is
rejected, the transaction rolls back and the claim disappears with it.

## Alternatives considered

| Decision | Alternative | Why not |
|---|---|---|
| Stored balance in `accounts` | `SUM()` over the user's ledger rows on every read | Reads a user's whole history. The seed has users with 150,000 rows; the brief rules this out |
| | Periodic snapshots plus summing the rows since the last snapshot | More moving parts and still a range scan per read, for no benefit over an exact running balance |
| | `balance_after` column on each ledger row, read the latest | Needs a `(user_id, id)` index (larger than the current one, because unique entries cannot be de-duplicated) and writers still have to serialise on something |
| | Materialized view | Refresh is not transactional with writes, so read-your-writes would break |
| Single-statement write guarded by the row lock | `SELECT ... FOR UPDATE`, check in Python, then `UPDATE` + `INSERT` | Same locking, but 4 or 5 round trips with the lock held throughout |
| | SERIALIZABLE isolation | Correct, but turns contention into aborted transactions that must be retried; worst exactly in the hot-user case |
| | Optimistic locking with a version column | Same retry storm under contention |
| | Advisory locks per user | Works, but the row lock already exists and is released automatically with the transaction |
| | Trigger on `ledger` that maintains `accounts` | Hides the core logic in the database and makes the 404/409 distinction awkward |
| | Stored procedure | Equivalent; plain SQL in the application is easier to read, test and review |
| `ledger_user_id_idx (user_id)` | No index on `ledger.user_id` | Slightly faster inserts, but auditing one user would mean a 10M-row scan |
| | `(user_id, id)` or `(user_id, created_at)` | Only pays off for a paginated history endpoint, which is not in scope |
| No foreign key `ledger.user_id -> accounts` | Add it | An extra lookup and row lock on every insert, for an invariant the single statement already guarantees |
| `type` as `text` + `CHECK` | Enum or smallint | Saves a few bytes per row but makes ad-hoc reviewer SQL (`type = 'credit'`) depend on casts |
| Unpartitioned `ledger` | Partition by time or by hash of user | No query here benefits. Time partitioning is the first thing I would add for archiving at 10x |
| asyncpg, hand-written SQL | An ORM | The whole design is three specific statements; an ORM would only obscure them |
| Manual JSON validation | Pydantic models | Pydantic's default mode coerces (`"100"` and `10.0` become `100`); the brief requires rejecting them |

## Seed

`docker compose run --rm seed` loads **10,076,000 ledger rows for 100,000
users**:

| Group | Users | Rows per user | Rows |
|---|---:|---:|---:|
| Heavy (`seed_0000001` to `seed_0000020`) | 20 | 150,000 | 3,000,000 |
| Regular | 1,980 | 1,000 | 1,980,000 |
| Long tail | 98,000 | 52 | 5,096,000 |

**It took 24.3 s on a MacBook Pro (Apple M1 Pro, Docker Desktop with 10 CPUs
and 8 GB)**: 20.9 s loading, 0.7 s `VACUUM ANALYZE`, 2.8 s verification. The
1M-row variant (`--scale 0.1`) took 2.6 s.

How it works ([scripts/seed.py](scripts/seed.py)):

- Rows are generated inside PostgreSQL with `generate_series`, in chunks of
  about 250,000 rows over 4 parallel connections.
- Each chunk is one statement that inserts the ledger rows and derives the
  account balances from those same rows, so `accounts` matches the ledger even
  if the seed is interrupted.
- For each user, every third row is a debit of 1 to 1,000 and the others are
  credits of 1,000 to 100,000. Every debit follows two credits that are each at
  least as large, so the running balance is never negative. This was checked
  with a window function over all 10M rows: the lowest running balance of any
  user at any point is 1,000, ordered by `id` or by `created_at`.
- The seed finishes by running the same reconciliation query as
  `scripts/reconcile.sql` and exits non-zero on any mismatch.
- It refuses to run twice on the same database.

## Tests

`docker compose run --rm tests` runs 157 tests against the running API and
database ([tests/](tests/)). They are safe to run on a seeded database: every
test uses fresh random user ids.

Concurrency tests ([tests/test_concurrency.py](tests/test_concurrency.py)), all
of which also compare the API balance with `SUM` over the ledger:

| Test | Brief rule |
|---|---|
| Two concurrent debits of 60 against 100 give one 200 and one 409 (40 fresh users) | 1. Non-negative balance |
| 1,000 concurrent credits of 100 raise the balance by exactly 100,000, and the 1,000 responses are 1,000 distinct balances | 2. No lost updates |
| 50 concurrent first credits create the user once | 2 |
| 200 concurrent debits of 10 against 1,000: exactly 100 succeed | 1 |
| 600 mixed concurrent credits and debits end at the expected balance | 1, 2, 3 |
| Every write is immediately visible to a read, across 30 parallel users | 4. Read-your-writes |
| 90 threads on one user while 10 threads work on other users | 5. Isolation across users |
| 2,000 mixed operations over 50 users; only 200/404/409 returned | 6. No silent failures |

The rest cover the API contract and every validation rule, authentication on
every endpoint (including that 401 comes before validation), idempotency keys
under concurrency, and the database-level guarantees (immutable ledger, CHECK
constraints).

## Load test results

`docker compose run --rm loadtest` runs three scenarios with 100 concurrent
clients for 30 s each (after a 5 s warm-up). Traffic is 40% balance reads, 35%
credits, 25% debits.

Same machine as above. The load generator runs in a container on the same
Docker VM, so it competes with the API and database for the 10 CPUs. Latency is
measured in the client, per request. Raw output is in [results/](results/).

**No request failed in any run** (only 200/404/409 returned), and
`scripts/reconcile.sql` returned zero mismatches after each run.

### Mixed traffic across many users

90% of requests go to random seeded users, 10% to new user ids (hence the 404s).

| Endpoint | p50 at 1M rows | p99 at 1M rows | p50 at 10M rows | p99 at 10M rows |
|---|---:|---:|---:|---:|
| `GET /balance` | 3.2 ms | 12.7 ms | 3.3 ms | 13.1 ms |
| `POST /credit` | 4.4 ms | 14.4 ms | 4.5 ms | 14.6 ms |
| `POST /debit` | 4.3 ms | 14.2 ms | 4.4 ms | 14.4 ms |
| Throughput | 22,279 req/s | | 21,459 req/s | |

Latency is the same at 1M and 10M rows, which is what the design predicts: no
request reads more than one account row, and writes append to the ledger.

### Hot user: all 100 clients on `seed_0000001` (10M rows)

| Endpoint | p50 | p99 |
|---|---:|---:|
| `GET /balance` | 0.6 ms | 1.0 ms |
| `POST /credit` | 25.6 ms | 42.2 ms |
| `POST /debit` | 26.0 ms | 42.0 ms |

Writes to one user are necessarily serial (about 4,100 per second here), so
each write waits its turn behind the others. Reads are unaffected.

### Isolation: 80 clients on the hot user, 20 on other users (10M rows)

| Endpoint | Hot user p50 / p99 | Other users p50 / p99 |
|---|---:|---:|
| `GET /balance` | 1.1 / 2.7 ms | 1.1 / 2.8 ms |
| `POST /credit` | 60.1 / 151.2 ms | 1.9 / 4.7 ms |
| `POST /debit` | 60.0 / 152.5 ms | 1.9 / 4.8 ms |

The hot user's queue does not leak into other users' latency.

## Query plans

Captured with `EXPLAIN (ANALYZE, BUFFERS)` on the 10M-row database, for the
heaviest user (150,000+ ledger rows). The write plans were run inside a
transaction that was rolled back.

### get_balance

```
Index Scan using accounts_pkey on accounts  (cost=0.42..2.64 rows=1 width=8) (actual time=0.038..0.039 rows=1 loops=1)
  Index Cond: (user_id = 'seed_0000001'::text)
  Buffers: shared hit=4
Planning Time: 0.268 ms
Execution Time: 0.063 ms
```

Four buffer reads; the ledger is not touched.

### credit

```
CTE Scan on acct  (cost=0.04..0.06 rows=1 width=8) (actual time=0.068..0.068 rows=1 loops=1)
  Buffers: shared hit=12
  CTE acct
    ->  Insert on accounts a  (cost=0.00..0.01 rows=1 width=40) (actual time=0.067..0.067 rows=1 loops=1)
          Conflict Resolution: UPDATE
          Conflict Arbiter Indexes: accounts_pkey
          Tuples Inserted: 0
          Conflicting Tuples: 1
          Buffers: shared hit=12
          ->  Result  (cost=0.00..0.01 rows=1 width=40) (actual time=0.001..0.001 rows=1 loops=1)
  CTE entry
    ->  Insert on ledger  (cost=0.00..0.03 rows=0 width=0) (actual time=0.125..0.125 rows=0 loops=1)
          Buffers: shared hit=56
          ->  CTE Scan on acct acct_1  (cost=0.00..0.03 rows=1 width=88) (actual time=0.025..0.025 rows=1 loops=1)
                Buffers: shared hit=10
Planning Time: 0.049 ms
Execution Time: 0.243 ms
```

An upsert through the primary key, then one ledger insert. No scan of any kind.
(The 56 buffers on the ledger insert are this session's first insert finding a
page with free space and opening both indexes; steady state is lower, as the
debit plan shows.)

### debit

```
Result  (cost=5.32..5.33 rows=1 width=9) (actual time=0.042..0.043 rows=1 loops=1)
  Buffers: shared hit=11
  CTE acct
    ->  Update on accounts  (cost=0.42..2.64 rows=1 width=14) (actual time=0.030..0.031 rows=1 loops=1)
          Buffers: shared hit=6
          ->  Index Scan using accounts_pkey on accounts  (cost=0.42..2.64 rows=1 width=14) (actual time=0.020..0.020 rows=1 loops=1)
                Index Cond: (user_id = 'seed_0000001'::text)
                Filter: (balance >= 100)
                Buffers: shared hit=4
  CTE entry
    ->  Insert on ledger  (cost=0.00..0.03 rows=0 width=0) (actual time=0.026..0.026 rows=0 loops=1)
          Buffers: shared hit=6
          ->  CTE Scan on acct  (cost=0.00..0.03 rows=1 width=88) (actual time=0.002..0.002 rows=1 loops=1)
                Buffers: shared hit=1
  InitPlan 3
    ->  CTE Scan on acct acct_1  (cost=0.00..0.02 rows=1 width=8) (actual time=0.031..0.031 rows=1 loops=1)
          Buffers: shared hit=6
  InitPlan 4
    ->  Index Only Scan using accounts_pkey on accounts accounts_1  (cost=0.42..2.64 rows=1 width=0) (actual time=0.010..0.010 rows=1 loops=1)
          Index Cond: (user_id = 'seed_0000001'::text)
          Heap Fetches: 1
          Buffers: shared hit=5
Planning Time: 0.060 ms
Execution Time: 0.085 ms
```

One primary-key index scan to find and lock the row, one ledger insert, and one
index-only lookup for the 404-versus-409 flag.

## Known limits

- **Single-user write throughput is serial.** All writes for one user go
  through one row lock, and each waits for its own commit to reach disk. That
  was about 4,100 writes per second for one user on this machine. This is the
  first thing to break at 10x load if the traffic is concentrated on a few
  users. The fix would be to batch a hot user's queued writes into one
  transaction (one disk flush for many operations).
- **At 10x evenly spread load**, the limits are, in order: API CPU (Python;
  add workers or instances), then PostgreSQL WAL flush throughput on the single
  primary. Database connections are bounded by pool size times workers (48 of
  `max_connections = 200`); with many instances a connection pooler (PgBouncer)
  would be needed.
- **Several API instances.** Correctness is unchanged, because it rests on
  database row locks and not on anything in process memory. What weakens is the
  hot-user fairness lock: it is per process, so a hot user could hold one
  connection per process across all instances. Remedies: route requests by
  user id hash at the load balancer, or cap waiting with `lock_timeout`.
  Migrations already take an advisory lock so instances can start together.
- **Read replicas** are not used. Reading balances from a replica would break
  read-your-writes unless replication is synchronous.
- **Table growth.** `ledger` grows without bound and is not partitioned.
  Reconciliation is a full scan (about 3 s at 10M rows).
- **Idempotency keys are kept forever.** A real system would expire them (for
  example after 24 hours).
- **Balance overflow.** A balance above 9.2 * 10^18 would overflow `bigint` and
  return a 500. That is 9.2 billion maximum-size credits to one user.
- **Request body size is not capped** by the application; a reverse proxy
  would normally do that.
- **Outcome unknown on connection loss.** If the database connection drops
  after a write was sent but before the reply arrived, the client gets a 503
  and cannot know whether the write was applied, unless it used an
  `Idempotency-Key` and retries.

## With more time

- Batch queued writes for the same user into one transaction (group commit),
  to lift hot-user throughput.
- Time-partition `ledger` and add an archiving policy.
- A paginated `GET /ledger/{user_id}` history endpoint, with the
  `(user_id, id)` index it needs.
- Expiry for idempotency keys, and replaying stored 4xx results as well.
- Metrics (latency histograms, pool wait time, lock wait time) and structured
  request logs.
- A scheduled reconciliation job that alerts on any mismatch.
- CI that runs the test suite and a short load test on every push.
- A load generator on a separate machine, so client and server do not share
  CPUs and the numbers reflect the server alone.
