"""Move `gitea` and `github` accounts to forge integrations, once, at API start
(docs/specs/forge-integrations.spec.md 3.6). It needs the secret store, so it is
not a SQL migration; it runs through the same create service as the API."""

import logging

from argus_core.accounts import REPOSITORY_PROVIDERS, repository_provider

from argus_api.db import Database, ts
from argus_api.errors import DomainError
from argus_api.events import record
from argus_api.integrations import service
from argus_api.secrets import SecretStore, SecretStoreError, ref_name
from argus_api.tasks.delivery import repositories

log = logging.getLogger(__name__)

ACTOR = "system:forge-move"
MOVED = "moved to forge integrations"
_FORGES = tuple(sorted(REPOSITORY_PROVIDERS))


async def move_forge_accounts(db: Database, store: SecretStore) -> None:
    """Copy the best enabled account of each project's forge provider into a
    `forge` integration of that project, then disable every forge account and
    release any repository lease still open. Projects that already have one are
    skipped, so a re-run after a crash finishes the job; with no forge account
    left enabled it does nothing."""
    async with db.read() as conn:
        async with conn.execute(
            "SELECT id, provider, secret_ref FROM accounts"
            " WHERE provider IN (?, ?) AND state != 'disabled'"
            " ORDER BY priority DESC, name",
            _FORGES,
        ) as cur:
            accounts = list(await cur.fetchall())
        if not accounts:
            return
        projects = await repositories(conn)
        async with conn.execute(
            "SELECT project FROM integrations WHERE kind = 'forge' AND state = 'active'"
        ) as cur:
            done = {r[0] for r in await cur.fetchall()}
    for project, (host, _, _) in sorted(projects.items()):
        if project in done:
            continue
        provider = repository_provider(host)
        for account in (a for a in accounts if a["provider"] == provider):
            name = ref_name(account["secret_ref"]) if account["secret_ref"] else None
            try:
                stored = store.get(name) if name else None
            except SecretStoreError:
                stored = None
            token = stored.get("token") if stored else None
            if stored is None or not isinstance(token, str):
                continue
            config = {"host": host}
            if provider == "gitea":
                config["username"] = str(stored.get("username") or "")
            try:
                await service.create(
                    db,
                    store,
                    project,
                    service.IntegrationCreate(
                        kind="forge",
                        name="forge",
                        access="read_write",
                        config=config,
                        secret=service.SecretIn(token=token),
                    ),
                    ACTOR,
                    migrated_from=account["id"],
                )
            except (DomainError, SecretStoreError) as exc:
                log.warning(
                    "could not move account %s to project %s: %s",
                    account["id"],
                    project,
                    getattr(exc, "code", type(exc).__name__),
                )
                continue
            break
        else:
            log.warning(
                "project %s has no %s account to move; add its forge integration",
                project,
                provider,
            )
    async with db.write() as tx:
        now = ts(tx.now)
        await tx.conn.execute(
            "UPDATE account_leases SET released_at = ?, release_reason = ?"
            " WHERE role = 'repository' AND released_at IS NULL",
            (now, MOVED),
        )
        for account in accounts:
            await tx.conn.execute(
                "UPDATE accounts SET state = 'disabled', disabled_reason = ?,"
                " exhausted_until = NULL, auto_disabled = 0, updated_at = ?"
                " WHERE id = ?",
                (MOVED, now, account["id"]),
            )
            await record(
                tx,
                ACTOR,
                "account",
                account["id"],
                "account.disabled",
                {"reason": MOVED},
            )
