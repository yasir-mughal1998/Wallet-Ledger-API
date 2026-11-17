"""Concurrency rules from the brief, exercised with real parallel HTTP requests.

Every test also reconciles the API's balance against the ledger table, because
"the balance equals credits minus debits" is the rule that fails a submission.
"""

import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from tests.conftest import ledger_balance, ledger_rows


def run_parallel(calls, threads=None):
    """Run zero-argument callables at the same time; return results in order."""
    with ThreadPoolExecutor(max_workers=threads or len(calls)) as pool:
        futures = [pool.submit(call) for call in calls]
        return [future.result() for future in futures]


def new_user() -> str:
    return "t_" + uuid.uuid4().hex


def test_two_concurrent_debits_only_one_succeeds(client):
    # Rule 1. Repeated on fresh users to give the race many chances to show up.
    for _ in range(40):
        user = new_user()
        client.post("/credit", json={"user_id": user, "amount": 100})

        debit = lambda: client.post("/debit", json={"user_id": user, "amount": 60})
        statuses = sorted(r.status_code for r in run_parallel([debit, debit]))

        assert statuses == [200, 409]
        assert client.get(f"/balance/{user}").json()["balance"] == 40
        assert ledger_balance(user) == 40
        assert ledger_rows(user) == 2  # the credit and the one accepted debit


def test_thousand_concurrent_credits_are_all_counted(client, user_id):
    # Rule 2: no lost updates.
    client.post("/credit", json={"user_id": user_id, "amount": 7})

    credit = lambda: client.post("/credit", json={"user_id": user_id, "amount": 100})
    responses = run_parallel([credit] * 1000, threads=100)

    assert all(r.status_code == 200 for r in responses)
    # Each response carries the balance right after its own operation, so the
    # 1,000 responses must be exactly the 1,000 distinct steps 107, 207, ...
    assert sorted(r.json()["balance"] for r in responses) == [7 + 100 * i for i in range(1, 1001)]
    assert client.get(f"/balance/{user_id}").json()["balance"] == 100_007
    assert ledger_balance(user_id) == 100_007
    assert ledger_rows(user_id) == 1001


def test_concurrent_first_credits_create_the_user_once(client, user_id):
    # The user does not exist yet: 50 requests race to create the account.
    credit = lambda: client.post("/credit", json={"user_id": user_id, "amount": 10})
    responses = run_parallel([credit] * 50)

    assert all(r.status_code == 200 for r in responses)
    assert client.get(f"/balance/{user_id}").json()["balance"] == 500
    assert ledger_balance(user_id) == 500


def test_concurrent_debits_never_overdraw(client, user_id):
    # 200 debits of 10 against a balance of 1,000: exactly 100 can succeed.
    client.post("/credit", json={"user_id": user_id, "amount": 1000})

    debit = lambda: client.post("/debit", json={"user_id": user_id, "amount": 10})
    statuses = [r.status_code for r in run_parallel([debit] * 200, threads=100)]

    assert statuses.count(200) == 100
    assert statuses.count(409) == 100
    assert client.get(f"/balance/{user_id}").json()["balance"] == 0
    assert ledger_balance(user_id) == 0
    assert ledger_rows(user_id) == 101


def test_mixed_credits_and_debits_match_the_ledger(client, user_id):
    client.post("/credit", json={"user_id": user_id, "amount": 500})
    rng = random.Random(42)
    operations = [(rng.choice(["credit", "debit"]), rng.randint(1, 300)) for _ in range(600)]

    def call(op, amount):
        return lambda: (op, amount, client.post(f"/{op}", json={"user_id": user_id, "amount": amount}))

    results = run_parallel([call(op, amount) for op, amount in operations], threads=100)

    expected = 500
    for op, amount, response in results:
        assert response.status_code in (200, 409)
        if response.status_code == 200:
            assert response.json()["balance"] >= 0
            expected += amount if op == "credit" else -amount
        else:
            assert op == "debit"

    assert expected >= 0
    assert client.get(f"/balance/{user_id}").json()["balance"] == expected
    assert ledger_balance(user_id) == expected


def test_read_your_writes(client):
    # Rule 4: as soon as a write returns 200, a read must reflect it. Run for
    # many users in parallel so reads land on other workers/connections.
    def scenario():
        user = new_user()
        expected = 0
        for step in range(1, 21):
            op, amount = ("credit", step * 10) if step % 4 else ("debit", step)
            written = client.post(f"/{op}", json={"user_id": user, "amount": amount})
            assert written.status_code == 200
            expected += amount if op == "credit" else -amount
            assert written.json()["balance"] == expected
            assert client.get(f"/balance/{user}").json()["balance"] == expected
        return True

    assert all(run_parallel([scenario] * 30))


def test_hot_user_does_not_block_other_users(client, user_id):
    # Rule 5: 90 threads hammer one user while 10 threads work on others.
    hot = user_id
    client.post("/credit", json={"user_id": hot, "amount": 1_000_000})
    stop_at = time.monotonic() + 3

    def hammer_hot_user():
        count = 0
        while time.monotonic() < stop_at:
            op = "credit" if count % 2 else "debit"
            assert client.post(f"/{op}", json={"user_id": hot, "amount": 5}).status_code == 200
            count += 1
        return []

    def work_on_other_users():
        latencies = []
        while time.monotonic() < stop_at:
            user = new_user()
            started = time.monotonic()
            assert client.post("/credit", json={"user_id": user, "amount": 50}).status_code == 200
            assert client.post("/debit", json={"user_id": user, "amount": 20}).status_code == 200
            assert client.get(f"/balance/{user}").json()["balance"] == 30
            latencies.append((time.monotonic() - started) / 3)
        return latencies

    results = run_parallel([hammer_hot_user] * 90 + [work_on_other_users] * 10)
    others = sorted(latency for result in results for latency in result)

    assert len(others) > 50, "requests for other users barely progressed"
    median = others[len(others) // 2]
    # Generous bound: a blocked request would wait behind 90 queued writers.
    assert median < 0.25, f"other users' median latency was {median * 1000:.0f} ms"
    assert ledger_balance(hot) == client.get(f"/balance/{hot}").json()["balance"]


def test_many_users_in_parallel_all_reconcile(client):
    # Mixed traffic across 50 users at once; every balance must match the ledger.
    users = [new_user() for _ in range(50)]
    rng = random.Random(7)
    plan = [(rng.choice(users), rng.choice(["credit", "credit", "debit"]), rng.randint(1, 500)) for _ in range(2000)]

    def call(user, op, amount):
        return lambda: client.post(f"/{op}", json={"user_id": user, "amount": amount}).status_code

    statuses = run_parallel([call(*step) for step in plan], threads=100)
    assert set(statuses) <= {200, 404, 409}  # 404: a debit that ran before the user's first credit

    for user in users:
        r = client.get(f"/balance/{user}")
        if r.status_code == 404:
            assert ledger_rows(user) == 0
        else:
            assert r.json()["balance"] == ledger_balance(user) >= 0
