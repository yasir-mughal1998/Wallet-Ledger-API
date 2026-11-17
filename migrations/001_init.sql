-- Wallet ledger schema.
--
-- ledger   : append-only source of truth, one row per credit/debit.
-- accounts : one row per user holding the current balance. It is only ever
--            changed in the same SQL statement that appends the ledger row,
--            so it always equals SUM(credits) - SUM(debits) for that user.

CREATE TABLE accounts (
    -- COLLATE "C" = plain byte comparison: user ids are ASCII-only, so
    -- locale-aware comparison would only make primary-key lookups slower.
    user_id text COLLATE "C" PRIMARY KEY,
    balance bigint NOT NULL CHECK (balance >= 0)
)
-- Leave free space in every page so a balance update can be written next to
-- the old row version (a HOT update) without touching the index.
WITH (fillfactor = 80);

CREATE TABLE ledger (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    text COLLATE "C" NOT NULL,
    type       text NOT NULL CHECK (type IN ('credit', 'debit')),
    amount     bigint NOT NULL CHECK (amount > 0),
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Not used by the API's hot path (balances come from accounts). It exists so
-- one user's history can be audited or re-summed without scanning every row.
CREATE INDEX ledger_user_id_idx ON ledger (user_id);

-- Ledger rows are immutable: reject UPDATE, DELETE and TRUNCATE in the database
-- itself, so no code path (or stray psql session) can rewrite history.
CREATE FUNCTION ledger_reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'ledger rows are immutable (% is not allowed)', TG_OP;
END;
$$;

CREATE TRIGGER ledger_immutable_rows
    BEFORE UPDATE OR DELETE ON ledger
    FOR EACH STATEMENT EXECUTE FUNCTION ledger_reject_mutation();

CREATE TRIGGER ledger_immutable_table
    BEFORE TRUNCATE ON ledger
    FOR EACH STATEMENT EXECUTE FUNCTION ledger_reject_mutation();

-- Optional Idempotency-Key support for POST /credit and POST /debit.
-- A row is written in the same transaction as the ledger row it belongs to,
-- so a key exists if and only if its operation was applied.
CREATE TABLE idempotency_keys (
    key        text COLLATE "C" PRIMARY KEY,
    type       text NOT NULL,
    user_id    text NOT NULL,
    amount     bigint NOT NULL,
    balance    bigint,          -- balance returned by the original request
    created_at timestamptz NOT NULL DEFAULT now()
);
