# Pool delete

# 1. Purpose and Scope

An operator can remove a worker pool definition that is no longer wanted.
Before this spec, pools could be created, edited and scaled to zero, but not
deleted: the row stayed forever. This spec adds `DELETE /api/pools/{name}`
(admin), an MCP tool, and a dashboard confirm dialog, with the same refusal
rules for system pools and in-use pools.

# 2. Non-Goals

- No automatic scale-down or terminate-all as part of delete: the operator
  scales or terminates first, then deletes when nothing live remains.
- No soft-delete or archive of pools; the row is removed.
- No change to how system pools (`orchestrator`) are created or disabled —
  those stay under orchestrator settings, not this DELETE.
- No cascade into agents, tasks, events or usage; only the pool row and its
  already-terminated instance rows go away.
- No schema migration.

# 3. Decisions Made

## 3.1 Admin-only DELETE with name confirmation

`DELETE /api/pools/{name}?confirm_name=` requires an admin token. `confirm_name`
must equal the pool's current name (`409 name_mismatch` otherwise), so a stale
or mistyped target is refused — same pattern as account delete.

## 3.2 System pools cannot be deleted

A pool with `system` set (the internal orchestrator) answers
`409 system_pool`. Disable or replace that pool through orchestrator settings,
not DELETE.

## 3.3 Live instances block delete

If any instance of the pool is not `terminated`, delete answers
`409 pool_in_use` with how many live instances remain. The operator must scale
to zero and/or terminate, wait until the reconciler has confirmed every
container gone, then delete. Delete never begins terminations itself.

## 3.4 Terminated instance rows are removed with the pool

`instances.pool` references `pools.name`, so terminated history for that pool
is deleted in the same write as the pool row. Agents that once ran on those
instances keep their rows (`instance_id` is not a foreign key). Events that
named the pool stay.

## 3.5 Surfaces

| Surface | Behaviour |
| --- | --- |
| REST | `DELETE /api/pools/{name}?confirm_name=` → `204` |
| MCP | `delete_pool(name, confirm_name)` (admin) |
| Dashboard | Delete on each non-system pool card; type-the-name dialog |

A successful delete records `pool.deleted` with `{name}`.

# 4. Open Questions

- None. Keeping terminated instance history under a renamed tombstone was
  considered and rejected: a deleted pool means the operator no longer wants
  that definition or its instance ledger.

# 5. Constraints

- Single-node SQLite write transaction; no network calls inside the write.
- Authorization matches other pool mutations: admin for DELETE.
- Cover every surface: REST, MCP, dashboard share `service.delete_pool`.

# 6. Implementation Consequences

- Spec: this file; link from `docs/README.md`. Brief pointer from
  [worker lifecycle](worker-lifecycle.spec.md) provisioning.
- API: `fleet.service.delete_pool`, `DELETE` on `fleet.routes`, OpenAPI regen.
- MCP: `delete_pool` in `mcp_server.py`.
- Dashboard: confirm dialog on the Pools page; hide Delete for system pools.
- Tests: service/route refusals (system, in use, name mismatch) and a clean
  delete that removes terminated instances; dashboard dialog wiring.

## Verification boundary

- Commands: `make check` (or the fleet and dashboard tests that cover delete).
- Verified: refusal codes and a successful delete against the API test DB;
  dashboard confirm enables only when the typed name matches.
- Not verified: a live Docker host teardown during delete (delete never talks
  to the target; termination already did).
