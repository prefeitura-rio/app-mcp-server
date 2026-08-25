"""Reusable fakes/helpers for the isolated resilience drill harness (plan
todo 8). Kept separate from `conftest.py` so drill test modules can import
these directly (`from src.tests.resilience.fakes import ...`) instead of
importing `conftest.py` itself as a module — `conftest.py` only wires these
into pytest fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).parent / "fixtures"

REDIS_URL_FIXTURE = "redis://:pw@mcp-redis:6379/0"


def load_fixture(name: str) -> Any:
    """Loads `src/tests/resilience/fixtures/<name>.json`.

    Kept as a thin, obvious wrapper (not a generic "fixture loader
    framework") so a missing/renamed fixture file fails with a plain
    `FileNotFoundError` naming the exact path — adversarial class:
    malformed/absent fixture input.
    """
    path = FIXTURES_DIR / f"{name}.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class SentinelFailoverRedisClient:
    """Fake `redis.asyncio.Redis` modeling a Sentinel/HAProxy failover
    timeline as a fixed number of `redis.ConnectionError`s followed by
    success — the same shape task 6's own
    `FakeRedisClient` in `test_redis_backend_failover.py` uses, reused here
    (not reimplemented differently) so both suites assert against one
    well-understood double.

    `connection_errors_before_recovery` models how many of the client's
    retry attempts land inside the Sentinel failover window (roughly
    `down-after-milliseconds` + election/promotion + HAProxy re-routing, per
    `docs/runbooks/redis-sentinel-failover.md#recovery-objectives`) before
    HAProxy starts routing to the newly promoted primary.
    """

    def __init__(self, connection_errors_before_recovery: int, recovered_value: Any):
        import redis

        self._error = redis.ConnectionError(
            "Error 111 connecting to mcp-redis:6379. Connection refused."
        )
        self._errors_remaining = connection_errors_before_recovery
        self._recovered_value = recovered_value
        self.calls = 0

    async def _next(self) -> Any:
        self.calls += 1
        if self._errors_remaining > 0:
            self._errors_remaining -= 1
            raise self._error
        return self._recovered_value

    async def get(self, _key: str) -> Any:
        return await self._next()

    async def set(self, **_kwargs: Any) -> Any:
        return await self._next()

    async def delete(self, _key: str) -> Any:
        return await self._next()

    async def ping(self) -> Any:
        return await self._next()
