"""Load test: p50 / p95 / p99 latency per endpoint under concurrent clients.

    docker compose run --rm loadtest                       # all scenarios, 100 clients
    docker compose run --rm loadtest --scenario hot --duration 60
    docker compose run --rm loadtest --users 10000         # after a --scale 0.1 seed

Scenarios
  mixed      credits, debits and balance reads spread over many users
             (90% seeded users, 10% brand-new users)
  hot        every client works on the same single user
  isolation  80% of the clients on one hot user, 20% on other users; latency
             is reported separately for the two groups

Clients are split over several processes so that the load generator itself is
not limited to one CPU core. Latency is measured per request, client side.
"""

import argparse
import asyncio
import multiprocessing
import os
import random
import sys
import time
from collections import defaultdict

import aiohttp

EXPECTED_STATUSES = {200, 404, 409}
HOT_USER = "seed_0000001"


def pick_request(scenario: str, group: str, rng: random.Random, users: int):
    """Return (label, method, path, json_body) for the next request."""
    if group == "hot":
        user = HOT_USER
    elif scenario == "mixed" and rng.random() < 0.10:
        user = f"lt_{rng.randrange(1_000_000):06d}"  # mostly never-seen users
    else:
        user = f"seed_{rng.randint(2, users):07d}"

    prefix = f"{group}: " if scenario == "isolation" else ""
    roll = rng.random()
    if roll < 0.40:
        return prefix + "GET /balance", "GET", f"/balance/{user}", None
    if roll < 0.75:
        return prefix + "POST /credit", "POST", "/credit", {"user_id": user, "amount": rng.randint(1, 5000)}
    return prefix + "POST /debit", "POST", "/debit", {"user_id": user, "amount": rng.randint(1, 5000)}


async def client_loop(session, base_url, scenario, group, users, warm_until, stop_at, samples, seed):
    rng = random.Random(seed)
    while time.monotonic() < stop_at:
        label, method, path, body = pick_request(scenario, group, rng, users)
        started = time.monotonic()
        try:
            async with session.request(method, base_url + path, json=body) as response:
                await response.read()
                status = response.status
        except (aiohttp.ClientError, asyncio.TimeoutError):
            status = 0  # connection-level failure
        finished = time.monotonic()
        if started >= warm_until:  # ignore the warm-up period
            samples.append((label, finished - started, status))


async def run_clients(base_url, token, scenario, groups, users, warmup, duration, seed):
    samples = []
    connector = aiohttp.TCPConnector(limit=0)  # one connection per client
    headers = {"Authorization": f"Bearer {token}"}
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(connector=connector, headers=headers, timeout=timeout) as session:
        warm_until = time.monotonic() + warmup
        stop_at = warm_until + duration
        await asyncio.gather(
            *(
                client_loop(session, base_url, scenario, group, users, warm_until, stop_at, samples, seed * 1000 + i)
                for i, group in enumerate(groups)
            )
        )
    return samples


def process_main(args):
    return asyncio.run(run_clients(*args))


def percentile(sorted_values, fraction):
    return sorted_values[min(len(sorted_values) - 1, int(len(sorted_values) * fraction))]


def run_scenario(scenario, options):
    if scenario == "hot":
        groups = ["hot"] * options.concurrency
    elif scenario == "isolation":
        hot = int(options.concurrency * 0.8)
        groups = ["hot"] * hot + ["others"] * (options.concurrency - hot)
        random.Random(1).shuffle(groups)  # spread both groups over the processes
    else:
        groups = ["others"] * options.concurrency

    per_process = [groups[i :: options.processes] for i in range(options.processes)]
    jobs = [
        (options.base_url, options.token, scenario, part, options.users, options.warmup, options.duration, seed)
        for seed, part in enumerate(per_process, start=1)
        if part
    ]
    with multiprocessing.Pool(len(jobs)) as pool:
        samples = [sample for chunk in pool.map(process_main, jobs) for sample in chunk]

    by_label = defaultdict(list)
    statuses = defaultdict(lambda: defaultdict(int))
    for label, latency, status in samples:
        by_label[label].append(latency)
        statuses[label][status] += 1

    unexpected = sum(
        count for counts in statuses.values() for status, count in counts.items() if status not in EXPECTED_STATUSES
    )
    print(f"\n### {scenario}: {options.concurrency} clients, {options.duration}s, {len(samples) / options.duration:,.0f} req/s")
    print("| endpoint | requests | p50 ms | p95 ms | p99 ms | max ms | statuses |")
    print("|---|---:|---:|---:|---:|---:|---|")
    for label in sorted(by_label):
        values = sorted(by_label[label])
        codes = ", ".join(f"{status}: {count}" for status, count in sorted(statuses[label].items()))
        print(
            f"| {label} | {len(values):,} | {percentile(values, 0.50) * 1000:.1f} | "
            f"{percentile(values, 0.95) * 1000:.1f} | {percentile(values, 0.99) * 1000:.1f} | "
            f"{values[-1] * 1000:.1f} | {codes} |"
        )
    print(f"\nunexpected responses (not 200/404/409): {unexpected}", flush=True)
    return unexpected


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://localhost:8080"))
    parser.add_argument("--token", default=os.environ.get("API_TOKEN", "recruitment-test-token"))
    parser.add_argument("--scenario", choices=["mixed", "hot", "isolation", "all"], default="all")
    parser.add_argument("--concurrency", type=int, default=100, help="concurrent clients (default 100)")
    parser.add_argument("--processes", type=int, default=4, help="load generator processes")
    parser.add_argument("--duration", type=int, default=30, help="measured seconds per scenario")
    parser.add_argument("--warmup", type=int, default=5, help="unmeasured seconds before each scenario")
    parser.add_argument("--users", type=int, default=100_000, help="number of seeded users (seed_0000001..)")
    options = parser.parse_args()

    scenarios = ["mixed", "hot", "isolation"] if options.scenario == "all" else [options.scenario]
    unexpected = sum(run_scenario(scenario, options) for scenario in scenarios)
    sys.exit(1 if unexpected else 0)


if __name__ == "__main__":
    main()
