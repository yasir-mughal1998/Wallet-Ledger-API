"""Optional extra: Idempotency-Key on POST /credit and POST /debit."""

import uuid
from concurrent.futures import ThreadPoolExecutor

from tests.conftest import ledger_balance, ledger_rows


def key() -> dict:
    return {"Idempotency-Key": uuid.uuid4().hex}


def test_retry_with_same_key_is_applied_once(client, user_id):
    headers = key()
    first = client.post("/credit", json={"user_id": user_id, "amount": 100}, headers=headers)
    client.post("/credit", json={"user_id": user_id, "amount": 1})  # balance moves on
    retry = client.post("/credit", json={"user_id": user_id, "amount": 100}, headers=headers)

    assert first.status_code == retry.status_code == 200
    assert retry.json() == first.json() == {"user_id": user_id, "balance": 100}  # original response replayed
    assert client.get(f"/balance/{user_id}").json()["balance"] == 101
    assert ledger_rows(user_id) == 2


def test_concurrent_requests_with_same_key_apply_once(client, user_id):
    client.post("/credit", json={"user_id": user_id, "amount": 1000})
    headers = key()

    def debit():
        return client.post("/debit", json={"user_id": user_id, "amount": 300}, headers=headers)

    with ThreadPoolExecutor(max_workers=30) as pool:
        responses = [f.result() for f in [pool.submit(debit) for _ in range(30)]]

    assert all(r.status_code == 200 for r in responses)
    assert all(r.json()["balance"] == 700 for r in responses)
    assert client.get(f"/balance/{user_id}").json()["balance"] == 700
    assert ledger_balance(user_id) == 700
    assert ledger_rows(user_id) == 2


def test_same_key_with_different_request_is_rejected(client, user_id):
    headers = key()
    client.post("/credit", json={"user_id": user_id, "amount": 100}, headers=headers)

    different_amount = client.post("/credit", json={"user_id": user_id, "amount": 200}, headers=headers)
    different_endpoint = client.post("/debit", json={"user_id": user_id, "amount": 100}, headers=headers)

    assert different_amount.status_code == different_endpoint.status_code == 400
    assert different_amount.json()["error"] == "invalid_request"
    assert client.get(f"/balance/{user_id}").json()["balance"] == 100


def test_rejected_operation_does_not_consume_the_key(client, user_id):
    headers = key()
    client.post("/credit", json={"user_id": user_id, "amount": 50})

    rejected = client.post("/debit", json={"user_id": user_id, "amount": 80}, headers=headers)
    assert rejected.status_code == 409

    client.post("/credit", json={"user_id": user_id, "amount": 50})
    accepted = client.post("/debit", json={"user_id": user_id, "amount": 80}, headers=headers)
    assert (accepted.status_code, accepted.json()["balance"]) == (200, 20)


def test_invalid_key_is_rejected(client, user_id):
    r = client.post("/credit", json={"user_id": user_id, "amount": 1}, headers={"Idempotency-Key": "x" * 256})
    assert (r.status_code, r.json()["error"]) == (400, "invalid_request")
    assert client.get(f"/balance/{user_id}").status_code == 404
