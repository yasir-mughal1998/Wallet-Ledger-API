-- Reconcile every balance against the ledger. Zero rows returned = consistent.
--
--   docker compose exec -T db psql -U wallet -d wallet < scripts/reconcile.sql
--
-- FULL JOIN catches all three failure modes: a balance that differs from the
-- ledger, an account with no ledger rows, and ledger rows with no account.

SELECT user_id,
       a.balance AS account_balance,
       l.balance AS ledger_balance
FROM accounts a
FULL JOIN (
    SELECT user_id,
           SUM(CASE WHEN type = 'credit' THEN amount ELSE -amount END) AS balance
    FROM ledger
    GROUP BY user_id
) l USING (user_id)
WHERE a.balance IS DISTINCT FROM l.balance;

-- No account may be negative (also enforced by a CHECK constraint).
SELECT count(*) AS negative_balances FROM accounts WHERE balance < 0;
