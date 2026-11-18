"""The one rule for "can an engine account serve a claim now" (account selection
3.6). Claim gate, overview, refill time, task release and the lease ranking all
read the registry through it, so none can disagree.

An account counts toward the engine pool only if it is an engine provider
(anthropic, openai, custom, local: never an executor-only one such as Cursor,
never the repository account). The local model has no quota, so it is never
held back; a disabled or unreachable account is not in the pool at all. It is *usable* when available, or exhausted past its reset;
*held back* while exhausted with a reset in the future or none stated.

A *credential-blocked* account (`LEASE_BLOCK_SQL`) is in neither count, like a
disabled one: its credential is dead, so leasing it only burns an attempt, and no
reset will fix that."""

from dataclasses import dataclass
from datetime import datetime

import aiosqlite
from argus_core.accounts import ENGINE_PROVIDERS, LOCAL_PROVIDER
from argus_core.custom_accounts import CustomConfig, serves_role

# Why an account is not leased though it is available (null: it is). Correlated on
# `a.id`. `credential_rejected`: its last quota probe was answered 401, until a probe
# reads or the credential is renewed. `auth_rejected`: a lease run reported an
# authentication rejection (`account.auth_rejected`) after the last accepted run,
# the last credential renewal, the last operator "Mark available"
# (`account.auth_cleared`) and the last successful probe. A probe that only
# lacks the usage scope (`oauth_scope_missing`) never blocks: the token serves.
LEASE_BLOCK_SQL = (
    "(SELECT CASE WHEN EXISTS (SELECT 1 FROM account_quota_probe p"
    "  WHERE p.account_id = a.id AND p.code = 'credential_rejected')"
    "  THEN 'credential_rejected'"
    "  ELSE (SELECT 'auth_rejected' FROM (SELECT max(e.at) AS rej FROM events e"
    "   WHERE e.subject_type = 'account' AND e.subject_id = a.id"
    "   AND e.kind = 'account.auth_rejected') r WHERE r.rej >"
    "   max(coalesce((SELECT max(u.at) FROM account_usage u WHERE u.account_id = a.id"
    "    AND u.measurement = 'measured' AND u.output_tokens > 0), ''),"
    "   coalesce((SELECT max(e.at) FROM events e WHERE e.subject_type = 'account'"
    "    AND e.subject_id = a.id AND e.kind IN ('account.credential_renewed',"
    "    'account.auth_cleared')), ''),"
    "   coalesce((SELECT p.at FROM account_quota_probe p WHERE p.account_id = a.id"
    "    AND p.status = 'updated'), ''))) END)"
)


@dataclass(frozen=True)
class EnginePool:
    usable: int
    exhausted: int
    next_reset: datetime | None  # earliest stated reset of a held-back account

    @property
    def awaiting_quota(self) -> bool:
        """Capacity is spent and none is left: a claim would only burn an attempt."""
        return self.exhausted > 0 and self.usable == 0


async def engine_pool(conn: aiosqlite.Connection, now: datetime | None) -> EnginePool:
    """`now` None: judge by state alone (an exhausted account stays held back until
    the recovery loop frees it), for readers that have no clock."""
    async with conn.execute(
        "SELECT a.provider, a.config, a.state, a.exhausted_until FROM accounts a"  # noqa: S608
        " WHERE a.state IN ('available', 'exhausted')"
        f" AND {LEASE_BLOCK_SQL} IS NULL"
    ) as cur:
        rows = await cur.fetchall()
    usable = exhausted = 0
    marks: list[datetime] = []
    for r in rows:
        if r["provider"] not in ENGINE_PROVIDERS:
            continue
        config = CustomConfig.model_validate_json(r["config"]) if r["config"] else None
        if not serves_role(r["provider"], config, "engine"):
            continue
        if r["provider"] == LOCAL_PROVIDER:
            usable += 1
            continue
        until = (
            datetime.fromisoformat(r["exhausted_until"])
            if r["exhausted_until"]
            else None
        )
        if r["state"] == "exhausted" and (until is None or now is None or until > now):
            exhausted += 1
            if until is not None:
                marks.append(until)
        else:
            usable += 1
    return EnginePool(usable, exhausted, min(marks, default=None))
