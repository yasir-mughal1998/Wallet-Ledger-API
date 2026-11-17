"""An in-process lock per key (here: per user id)."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager


class KeyedLock:
    """Lets one coroutine at a time run for a given key; other keys are unaffected.

    This is NOT what makes balances correct - the database row lock does that,
    and it also holds across processes and instances. This lock only decides
    where a hot user's writes queue up: here, in memory, instead of each one
    holding a database connection while it waits for the same row lock. Without
    it, 100 concurrent writes to one user would occupy the whole connection
    pool and stall requests for every other user.
    """

    def __init__(self) -> None:
        # key -> [lock, number of coroutines holding or waiting for it]
        self._entries: dict[str, list] = {}

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncGenerator[None]:
        entry = self._entries.get(key)
        if entry is None:
            entry = self._entries[key] = [asyncio.Lock(), 0]
        entry[1] += 1
        try:
            async with entry[0]:  # asyncio.Lock wakes waiters in FIFO order
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                # Nobody holds or waits: drop the entry so the dict stays small.
                del self._entries[key]

    def __len__(self) -> int:
        return len(self._entries)
