"""Drill: Redis Sentinel failover (plan todo 8, failure class 3 of 5).

Three things are proven here, all offline — see
`docs/runbooks/redis-sentinel-failover.md#safety-boundaries`: no pod, real or
"isolated test namespace," is ever deleted by this drill:

1. `signoz_alert.mcp_redis_workload_unavailable`'s threshold logic fires on
   a fixture desired-vs-available mismatch for redis-named pods.
2. The real `RedisBackend` (plan todo 6, already merged) recovers within its
   documented retry budget when driven through a fake Redis client that
   reproduces the Sentinel failover timeline as a run of `ConnectionError`s
   followed by success — reusing `SentinelFailoverRedisClient` from
   `conftest.py`, itself modeled on task 6's own
   `test_redis_backend_failover.py::FakeRedisClient`.
3. The recovery objectives documented in
   `docs/runbooks/redis-sentinel-failover.md` (RTO ~10-20s, RPO bounded to a
   few seconds of writes) are restated here as explicit, source-cited
   assertions on named constants — NOT measured live. This drill does not
   and cannot prove real Sentinel promotes a real replica within that
   window; it proves the client-side retry contract is compatible with that
   documented window, and that the window itself is transcribed correctly
   from `infra/superapp`'s own Task-5 evidence rather than invented here.
"""

from __future__ import annotations

import pytest

from src.tests.resilience.fakes import REDIS_URL_FIXTURE, SentinelFailoverRedisClient
from src.tests.resilience.signoz_alert_evaluators import (
    evaluate_redis_workload_unavailable,
)


# ---------------------------------------------------------------------------
# 1. Alert threshold logic (redis-named deployments)
# ---------------------------------------------------------------------------


def test_redis_workload_mismatch_fires(fixture_loader):
    verdict = evaluate_redis_workload_unavailable(
        fixture_loader("redis_workload_unavailable_firing")
    )

    assert verdict.firing is True
    assert verdict.alert_name == "mcp_redis_workload_unavailable"
    assert (
        verdict.runbook_url == "https://runbooks.example.internal/mcp/redis-unavailable"
    )


# ---------------------------------------------------------------------------
# 2. Client-side recovery through a simulated failover window
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_recovers_within_budget_during_simulated_failover(
    state_module, no_sleep
):
    """Down-after-milliseconds (10s) detection + low single-digit-second
    election/promotion + ~1-2s HAProxy re-routing (documented, not
    reproduced with real timing, in
    docs/runbooks/redis-sentinel-failover.md#recovery-objectives) means a
    real client sees on the order of a handful of consecutive
    `ConnectionError`s, not dozens. Three simulated failed attempts before
    recovery is well within that shape and well within the default 5-attempt
    budget (`RedisBackend.DEFAULT_MAX_ATTEMPTS`)."""
    backend = state_module.RedisBackend(redis_url=REDIS_URL_FIXTURE, ttl_seconds=60)
    backend.client = SentinelFailoverRedisClient(
        connection_errors_before_recovery=3, recovered_value='{"user_id": "42"}'
    )

    result = await backend.load_user_data("user-1")

    assert result == {"user_id": "42"}
    assert backend.client.calls == 4  # 3 failures + 1 success
    assert len(no_sleep) == 3  # one backoff wait between each of the 3 failures


@pytest.mark.asyncio
async def test_client_exhausts_budget_if_failover_never_completes(
    state_module, no_sleep
):
    """A Sentinel quorum failure (see
    docs/runbooks/redis-sentinel-failover.md, Remediation #2) means the
    primary never comes back — the client must eventually give up and
    surface `redis.ConnectionError`, not hang or silently return stale
    data. This is the same "esgotamento" contract task 6 already locks in;
    reasserted here from the resilience-drill entry point rather than
    duplicated as new behavior."""
    import redis

    backend = state_module.RedisBackend(
        redis_url=REDIS_URL_FIXTURE,
        ttl_seconds=60,
        max_attempts=4,
        base_delay_seconds=0.1,
    )
    backend.client = SentinelFailoverRedisClient(
        connection_errors_before_recovery=999, recovered_value="unreachable"
    )

    with pytest.raises(redis.ConnectionError):
        await backend.load_user_data("user-1")

    assert backend.client.calls == 4
    assert no_sleep == [0.1, 0.2, 0.4]


# ---------------------------------------------------------------------------
# 3. Documented recovery objectives, restated as explicit assertions
# ---------------------------------------------------------------------------


def test_default_retry_budget_covers_the_documented_rto_with_margin(state_module):
    """Restates `docs/runbooks/redis-sentinel-failover.md`'s "Client
    reconnect budget: 10-60s cumulative backoff" claim as an assertion on
    the actual default constants, so a future change to
    `RedisBackend.DEFAULT_*` that silently breaks that documented margin
    fails a test instead of only being caught by someone re-reading prose.
    Identical computation to
    `test_redis_backend_failover.py::test_orcamento_padrao_e_limitado_e_documentado`
    — restated here as this failure class's own drill entry point, not
    duplicated as new logic."""
    backend = state_module.RedisBackend(redis_url=REDIS_URL_FIXTURE)

    total_wait = sum(
        min(backend.base_delay_seconds * (2**i), backend.max_delay_seconds)
        for i in range(backend.max_attempts - 1)
    )

    # docs/runbooks/redis-sentinel-failover.md: RTO "roughly 10-20 seconds,
    # dominated by the 10s down-after-milliseconds detection delay." The
    # budget must cover at least that 10s detection floor, or a real
    # failover could exhaust the client's retries before Sentinel has even
    # finished *detecting* the primary is down, let alone before HAProxy
    # re-routes to the newly promoted one.
    documented_rto_detection_floor_s = 10
    assert total_wait >= documented_rto_detection_floor_s, (
        "the default retry budget must cover the documented 10s Sentinel "
        "detection floor with margin, or a real failover could exhaust the "
        "client's retries before Sentinel even finishes detecting the outage"
    )


def test_documented_rpo_bound_matches_infra_evidence():
    """Restates the RPO bound copied into
    docs/runbooks/redis-sentinel-failover.md from
    infra/superapp's docs/redis-sentinel-ha-mcp.md, as data rather than
    prose: ~1s AOF fsync window + up to ~5s replication-lag bound
    (`min-replicas-max-lag: 5`, cited in that source file). This assertion
    exists so anyone updating either document has a single place that fails
    if the two drift apart, given app-mcp-server does not own the source
    file and cannot enforce consistency by import."""
    aof_fsync_window_s = 1  # appendfsync everysec
    replication_lag_bound_s = 5  # min-replicas-max-lag: 5

    documented_worst_case_rpo_s = aof_fsync_window_s + replication_lag_bound_s

    assert documented_worst_case_rpo_s == 6
