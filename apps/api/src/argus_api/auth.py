"""Bearer-token authentication and admin tokens (worker lifecycle 3.6)."""

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import aiosqlite
from argus_core.auth import Scope

from argus_api.agents.tokens import ADMIN_PREFIX, hash_token, new_token
from argus_api.db import Tx, ts
from argus_api.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    UnauthorizedError,
)

log = logging.getLogger(__name__)

MIN_ADMIN_TOKEN_LENGTH = 32


@dataclass(frozen=True)
class Identity:
    token_id: str
    scope: Scope
    agent_id: str | None
    label: str | None

    @property
    def actor(self) -> str:
        if self.agent_id is not None:
            return f"agent:{self.agent_id}"
        return f"admin:{self.label or self.token_id[:8]}"


async def authenticate(
    conn: aiosqlite.Connection,
    token: str,
    *,
    now: datetime | None = None,
) -> Identity:
    """Resolve an agent or admin token. Bootstrap, revoked, unknown tokens, and
    tokens of agents that have ended, all fail."""
    async with conn.execute(
        "SELECT t.id, t.kind, t.role, t.agent_id, t.label, t.expires_at FROM tokens t"
        " LEFT JOIN agents a ON a.id = t.agent_id"
        " WHERE t.token_hash = ? AND t.revoked_at IS NULL AND ("
        "  t.kind = 'admin' OR (t.kind = 'agent' AND a.state IN ('active', 'draining')))",
        (hash_token(token),),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise UnauthorizedError("unknown, revoked, or ended token")
    if row["expires_at"] is not None:
        expires = datetime.fromisoformat(row["expires_at"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        current = now if now is not None else datetime.now(UTC)
        if expires <= current:
            raise UnauthorizedError("token expired", code="token_expired")
    return Identity(row["id"], Scope(row["role"]), row["agent_id"], row["label"])


# -- admin tokens -------------------------------------------------------------------


class AdminTokenConfigError(DomainError):
    pass


async def ensure_admin_token(tx: Tx, data_dir: Path, preset: str | None) -> str | None:
    """Make sure an admin can get in. Returns a newly generated token, if any.

    - `preset` (ARGUS_ADMIN_TOKEN) is registered if Argus has never seen it.
      A preset that was revoked stays revoked.
    - Otherwise, if no live admin token exists, one is generated and written to
      `<data_dir>/admin-token` (mode 0600) inside the transaction, so a failed
      write leaves nothing behind.
    """
    if preset is not None:
        if len(preset) < MIN_ADMIN_TOKEN_LENGTH:
            raise AdminTokenConfigError(
                f"ARGUS_ADMIN_TOKEN must be at least {MIN_ADMIN_TOKEN_LENGTH} characters"
            )
        async with tx.conn.execute(
            "SELECT 1 FROM tokens WHERE token_hash = ?", (hash_token(preset),)
        ) as cur:
            if await cur.fetchone() is None:
                await _insert_admin(tx, preset, "preset", "system")
        return None
    if await _live_admin_count(tx.conn) > 0:
        return None
    token = new_token(ADMIN_PREFIX)
    await _insert_admin(tx, token, "initial", "system")
    path = data_dir / "admin-token"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)  # before writing, in case the file already existed
        with os.fdopen(fd, "w") as f:
            fd = -1  # fdopen owns it
            f.write(token + "\n")
    finally:
        if fd >= 0:
            os.close(fd)
    return token


@dataclass(frozen=True)
class AdminToken:
    id: str
    label: str | None
    created_by: str
    created_at: datetime
    revoked_at: datetime | None


async def create_admin_token(tx: Tx, label: str, actor: str) -> tuple[str, str]:
    token = new_token(ADMIN_PREFIX)
    token_id = await _insert_admin(tx, token, label, actor)
    return token_id, token


async def list_admin_tokens(conn: aiosqlite.Connection) -> list[AdminToken]:
    async with conn.execute(
        "SELECT id, label, created_by, created_at, revoked_at FROM tokens"
        " WHERE kind = 'admin' ORDER BY created_at, id"
    ) as cur:
        return [
            AdminToken(
                r["id"],
                r["label"],
                r["created_by"],
                datetime.fromisoformat(r["created_at"]),
                datetime.fromisoformat(r["revoked_at"]) if r["revoked_at"] else None,
            )
            for r in await cur.fetchall()
        ]


async def revoke_admin_token(tx: Tx, token_id: str) -> None:
    async with tx.conn.execute(
        "SELECT revoked_at FROM tokens WHERE id = ? AND kind = 'admin'", (token_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"admin token not found: {token_id}")
    if row["revoked_at"] is not None:
        return
    if await _live_admin_count(tx.conn) <= 1:
        raise ConflictError(
            "refusing to revoke the last admin token; create another first",
            code="last_admin_token",
        )
    await tx.conn.execute(
        "UPDATE tokens SET revoked_at = ? WHERE id = ?", (ts(tx.now), token_id)
    )


async def _insert_admin(tx: Tx, token: str, label: str, actor: str) -> str:
    token_id = str(uuid4())
    await tx.conn.execute(
        "INSERT INTO tokens (id, token_hash, kind, role, label, created_by, created_at)"
        " VALUES (?, ?, 'admin', 'admin', ?, ?, ?)",
        (token_id, hash_token(token), label, actor, ts(tx.now)),
    )
    return token_id


async def _live_admin_count(conn: aiosqlite.Connection) -> int:
    async with conn.execute(
        "SELECT count(*) FROM tokens WHERE kind = 'admin' AND revoked_at IS NULL"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    return row[0]
