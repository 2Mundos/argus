# Pool editor

# 1. Purpose and Scope

The dashboard's pool editor edits an existing pool (`PATCH /api/pools/{name}`)
and creates a plain pool (`PUT /api/pools/{name}`). Before this spec its only
image field set the pool's top-level `image`, so an operator could not tell the
**host image** from the **NanoClaw agent image** (`nanoclaw.agent_image`), and
putting an agent image in the host field made worker startup fail.

This spec defines which settings the editor exposes, where its image choices
come from, and how it handles stale versions and server errors.

# 2. Non-Goals

- No change to authorization: PATCH and PUT stay admin-only, env values stay
  redacted unless `reveal_env` allows it, the NanoClaw socket policy still runs
  server-side. The editor adds no credential handling.
- No editing of `target`, `runtime`, `template` or `mode` of an existing pool.
- No editing of `node_selector`, `image_pull_secrets`, `service_account`,
  NanoClaw paths (`install_dir`, `instances_root`, `agent_image_archive`) or
  gateway options. They are preserved, not shown.
- No NanoClaw pool creation in this editor; the New worker flow does that.
- No schema migration.
- No use of other pools' agent images as a catalog.

# 3. Decisions Made

## 3.1 Two image roles

| Field | Stored in | Shown when |
| --- | --- | --- |
| Host image | `image` | always |
| NanoClaw agent image | `nanoclaw.agent_image` | runtime is `nanoclaw` |

Each has its own label and help text saying which one it is. The editor never
offers one role's image as a choice for the other, and refuses a save where the
host image equals the agent image or equals the catalog's image for the other
role.

## 3.2 Image sources

Choices come only from `GET /api/fleet/templates` (manager scope), for the
pool's own node (`local`, or `docker.host`):

- NanoClaw host and agent images: `Readiness.host_image` and
  `Readiness.agent_image` (new, `RuntimeImage`: `image`, `source: "config"`,
  `setting`, `present`, `detail`). They report `ARGUS_NANOCLAW_HOST_IMAGE` and
  `ARGUS_NANOCLAW_AGENT_IMAGE` and the existing readiness checks. For the
  sandbox template `agent_image.present` is `null`: the image is loaded from an
  archive into the instance's own daemon and is not checked on the node.
- Plain host images: `Readiness.images` (Argus worker images).
- Plain pools have no verified agent image list.

Authorization of the catalog is unchanged (manager only; a worker token gets
403).

## 3.3 Empty, error and manual states

- **Loading, error, empty, unknown node, Kubernetes target, plain agent role**:
  the field is a text input with a hint saying why there is no list. Error
  offers Retry. Nothing replaces the stored value.
- **Options available**: a select with the options, the stored value (marked
  "current, not in the catalog" when absent) and "Other: type a reference…".
- A changed image is validated as a Docker reference (max 500 characters).
  A stored value that was not changed is never validated or re-sent.

## 3.3a Host/agent role invariant (server and degraded catalog)

NanoClaw pools keep host (`image`) and agent (`nanoclaw.agent_image`) distinct:

- **Server**: `PoolUpsert` rejects `image == nanoclaw.agent_image`
  (`invalid_pool` on PATCH merge; request validation on PUT). When
  `ARGUS_NANOCLAW_HOST_IMAGE` / `ARGUS_NANOCLAW_AGENT_IMAGE` are set, the API
  also refuses putting the configured agent image in `image` or the configured
  host image in `nanoclaw.agent_image` (settings only; no network call).
- **Editor, catalog degraded**: the editor keeps the last successfully loaded
  catalog for cross-role checks even if a later reload fails. When no catalog
  has ever loaded, it still requires the two typed values to differ and shows
  an explicit hint that role names cannot be verified until the catalog is
  available — it does not treat an unverified value as verified.

## 3.4 What a save sends

Edit sends **only changed fields** plus `expected_version`.

- `nanoclaw` is replaced whole by the API, so the editor sends the stored
  object with `agent_image` replaced, and only when the agent image changed.
  `docker` and `kubernetes` are likewise merged over the stored object.
- `env` is a merge patch: a changed value sets, a removed key is `null`,
  untouched keys are omitted. Redacted values (`***`) are never sent; typing
  `***` for a key with no stored value is a form error.
- A save with no change closes without a request.

## 3.5 Concurrency and errors

- `409 stale_version` keeps the draft and offers "Keep my edits on the latest
  version" (re-base: the user's changed fields win, others take the latest
  values) or "Discard my edits and reload". After either action, each image
  field's control matches the current value (no stale "Other: type a
  reference…" local state from before the rebase).
- `422` validation paths are mapped to the field they belong to and shown next
  to it; unmapped errors show on the form. The draft is kept for every error.

## 3.6 Create

Create is for plain pools and builds the full PUT body from the same fields.

# 4. Open Questions

- Should the API expose the agent images other pools already use as choices?

# 5. Constraints

- Single-node first: no new service; the catalog is computed from settings and
  existing node probes.
- The editor talks only to the API.
- Layout works from 390px wide: single column, labels wrap, no horizontal scroll.

# 6. Implementation Consequences

- `argus_core.fleet`: `RuntimeImage`, `Readiness.host_image`, `.agent_image`;
  `fleet/templates.py` fills them; OpenAPI and the TS client are regenerated.
- Dashboard: `pools.ts` (form model, validation, `buildPool`, `imageChoices`,
  `poolErrors`), `views/PoolEditor.tsx`, `api.ts` (FastAPI 422 detail arrays).
- Tests: `pools.test.ts`, `views/PoolEditor.test.tsx`, `test_templates.py`.
