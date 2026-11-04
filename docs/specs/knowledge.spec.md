# Knowledge Spec

Finalized from [the 2026-10-14 draft](../drafts/2026-10-14.knowledge.draft.md),
which was inspired by Radar's shared scenario knowledge and reduced to fit
[lightweight single-node](single-node.spec.md).

# 1. Purpose and Scope

A shared, versioned wiki that agents and humans both read and improve, so the
fleet stops rediscovering the same facts: how a project builds and deploys,
conventions, decisions already made, pitfalls. Knowledge outlives tasks; a
task's spec and clarifications are what one task needs.

- **Humans** browse, search, edit, restore, and archive pages in the dashboard.
- **Agents** receive relevant page references with every claim, search and read
  pages, and contribute pages and edits (published or proposed, per setting).
- **Orchestrators** curate like admins and resolve proposals in the inbox.

# 2. Non-Goals

- Claim-level evidence and structural validation (Radar's `claims`). Pages are
  authored Markdown with page-level provenance (`sources`).
- Elasticsearch, embeddings, or a vector store. Search is SQLite FTS5.
  Semantic search may come later as an optional integration (single-node 3.5).
- Per-page permissions, attachments, real-time co-editing.
- Changing agent instructions or permissions. Knowledge informs; it never grants.

# 3. Decisions Made

## 3.1 Projects

`projects` (`slug`, `name`, `description`, optional `repository_url`) are a
first-class scope, managed by orchestrators and admins (`PUT /api/projects/{slug}`;
MCP `set_project_repository`; only an admin may change the URL, because it decides
which host a repository credential is sent to: `403 repository_admin_only`
otherwise). `PATCH /api/projects/{slug}/repository` (admin only, body
`{"repository_url": url|null}`, field required) changes just the repository in one
write: it never reads or writes name or description, so it cannot overwrite a
concurrent edit of them; the dashboard and MCP `set_project_repository` use it.
`global` has none (422 `project_global`); an unknown project is 404. `repository_url` is the project's code, the
authoritative source for its repository: an HTTPS `owner/repository` URL without
credentials, query or fragment (422 otherwise). Omitted from a PUT it is kept; an
**admin's** explicit null clears the stored URL. JSON null from a non-admin on
update is treated as omitted (so clients that round-trip null do not clear a URL).
On **create**, a project with no URL inherits the single agreed pool-map entry
(same rule as migration 0071); only an admin's explicit null on create skips that
bootstrap. **One resolver.** Create-time checks, delivery, the claim and the stats
all accept a stored or pool repository only if it canonicalises (HTTPS, no
credentials, a real `owner/repo`; `https://host/owner/.git` does not), so a value
one rejects is rejected by all; a usable pool mapping still stands in for an unusable
record. It is a pointer, not access: a task still needs a registered repository
account and gets a task-scoped lease. The claim response carries it as
`project_repository` (resolved in the same write transaction as the claim, from the
project record else the one URL every pool allowlist agrees on; null when unknown
and for `global`), so an edit reaches the next claim without restarting a worker or
editing a pool. The channel prefers it over its pool map and scopes it to the task:
it is the whole OneCLI allowlist for that task and is added to the helpers'
`ARGUS_REPOSITORIES_JSON` only until the task releases its repository.
**Automatic bootstrap.** Migration 0071 copies each existing project's URL from
pool allowlists when every pool that names it agrees; new projects inherit on create
the same way. **First-use gap:** when no pool names the project, or pools disagree,
there is no authoritative URL—Argus never guesses. An admin must set
`repository_url` (REST or MCP `set_project_repository`) before branch/PR deliverables
or write tasks can run; read-only code availability and claims stay without a tree
until then. `GET /api/projects/stats` (orchestrator or admin) returns per project the
repository Argus resolves (the canonical URL) and `repository_source`: `project`
(a usable record), `pools` (every pool that names it agrees), `conflict` (pools name
different repositories: Argus picks none, though a worker whose own pool names one
may still use it), `invalid` (a stored value that cannot be canonicalised, with no
pool to stand in) or null (nothing names one). It also returns live task counts by
state, archived tasks, the newest task update and active knowledge pages, all counted
from Argus's own tables and computed from one read of the pools, not one per project.
Nothing is estimated; a project with no tasks reports empty counts and no activity
time. `global` exists by
default as the scope of fleet-wide knowledge and settings; it is **not a home
for tasks**. **Every task belongs to a real project**: it is required on
creation, except that a subtask without one inherits its parent's. Naming
`global`, or omitting the project with no parent, is `422 project_required`.
Work that spans projects goes in a project made for it (for example `argus` or
`ops`). Tasks created before this rule stay in `global`, and their subtasks
inherit it. Routine templates and draft publishes follow the same rule. No
per-project permissions: any valid token reads every project.

### 3.1.1 Dashboard

- **Projects page** (`#projects`, in the primary nav) lists every project with its
  repository, the source when the pools supply it, and the stats above. Setting,
  changing or clearing the repository opens one dialog that reads the project (to
  prefill it), sends `PATCH /api/projects/{slug}/repository` (name and description are
  never sent or touched), and shows the API's refusal in plain words (`repository_admin_only`, 422 shape errors,
  an unknown project, no connection). The dashboard does not know the token's role,
  so every user sees the edit control and a non-admin is refused on save. `global`
  has no repository and no edit control. If the stats fail, the page says so and
  shows only what the project records hold.
- **New task** warns when the selected project has no usable repository, following
  the selection, and never blocks Create. The wording follows what is true: Argus
  refuses to create a branch/PR task without one (`deliverable_repository`); a task
  holding `repository:write` (typed, or the kind's default capabilities while the
  field is empty) is blocked by a worker that cannot resolve one; with
  `conflict` the warning says workers whose pool names one may use it and others
  block; `invalid` says the stored value is unusable; anything else says the task
  can still run without a checkout. "Set repository…" opens the same dialog beside the
  form (a portal, never a form inside the form), so nothing typed is lost on save,
  cancel or error. A saved repository counts at once, until the stats are re-read.
  When the stats cannot be read the form says the repository is unknown instead of
  claiming it is missing.
- **Escape and focus.** The dialog takes focus and handles Escape itself, stopping
  it from reaching the drawer or page behind.
- **Blocked on a missing repository.** A task blocked with
  `env_repository_unconfigured` shows, in the inbox card and the task drawer, the
  project's current state: no repository ("Set repository…"), conflicting pools or an
  unusable value (set one), or a repository that resolves now, possibly set elsewhere
  since the block (an "Unblock task" button with no save needed). After a save,
  "Unblock task" appears. Unblocking is always an explicit click, single-flight; once
  it succeeds the controls are replaced by a confirmation, and a 409 (someone already
  moved the task) is shown as "already unblocked", not as an error. The worker
  protocol is unchanged: no new decision kind is opened because answering a decision
  cannot change a project's repository. The inbox's actionable `next` names
  `set_project_repository` for an admin and `escalate_to_human` for anyone else
  ([MCP operations](mcp-operations.spec.md) 3.2).

## 3.2 Pages and revisions

- **`knowledge_pages`**: `project`, `slug` (unique per project; derived from the
  title if not given), current `title`/`summary`/`tags` mirrored from the
  current revision for listing, `current_revision_id`, `state` (`active` |
  `archived`), `reviewed_at`.
- **`knowledge_revisions`**: immutable; `number` is server-assigned and only
  grows per page; `title`, `summary` (one line, shown in search results and
  claims), `body` (Markdown), `tags`, `author`, `reason`, `sources` (task,
  decision, artifact, or URL references).
- **Edits are a compare-and-swap**: an edit names `base_revision_id`; if the
  page has moved on, it fails with `409 knowledge_conflict` ("re-read it and
  apply your change again") instead of overwriting. With one writer, the check
  is a compare inside the write transaction.
- **Restore** publishes a new revision carrying an old revision's content and a
  reason; history is never rewound.
- **Archive** hides a page from search and claims and blocks edits until
  unarchived; it stays readable.

## 3.3 Who writes

| Actor | Can |
| --- | --- |
| Admin, orchestrator | Create and edit pages, restore, archive/unarchive, mark reviewed, resolve proposals, manage projects. |
| Worker | Create and edit pages, which **publish** or are **proposed** according to `ARGUS_KNOWLEDGE_WORKER_WRITES` (`propose`, the default, or `publish`). The response says which (`status: published | proposed`). |

A proposal is stored in `knowledge_proposals` and opens a `knowledge` decision
(audience `any`, options `accept`/`reject`) in the existing inbox. Accepting
publishes it credited to the proposing agent, still compare-and-swap: a
proposal whose base revision moved fails with `knowledge_conflict` and its
decision stays open, so the reviewer can reject it and ask for a fresh one.

Published worker edits keep author, reason, and sources, so they can be
reviewed and restored afterwards.

**Save to knowledge.** Answering a worker's question can also keep the answer
(`save_to_knowledge`): appended to an existing page as a new revision, or as a
new page in the task's project (or `global`), with the decision and task as
sources.

## 3.4 How agents use it

- **On claim**: `POST /api/tasks/claim` returns `knowledge_refs`, at most five
  references (id, project, title, summary, stale, why): pinned pages first,
  then the best full-text matches for the task's title in its project, then in
  `global`. Pages are pinned on the task (`TaskCreate.knowledge`) or on its kind
  (`TaskKind.default_knowledge`, e.g. the deploy runbook for every `deploy`
  task); kind pins merge with the task's own. References only, so the claim
  stays small and the agent decides what to read. When the NanoClaw channel
  inlines page bodies into the worker prompt, they are untrusted DATA inside a
  length-framed JSON block ([prompt-data isolation](prompt-data-isolation.spec.md)),
  never trusted instructions.
- **Search**: `GET /api/knowledge/search?q=&project=` over title, summary, body,
  and tags (FTS5 with Porter stemming, ranked by bm25 weighted towards titles
  and tags), all words required, falling back to any word. User input is
  tokenized and quoted, so it can never be interpreted as FTS5 syntax.
- **Read**: `GET /api/knowledge/pages/{id}` (or `?revision=N`), history at
  `/revisions`.
- MCP tools (`search_knowledge`, `get_knowledge_page`, `write_knowledge`) map
  onto these when the MCP endpoint lands.

## 3.5 Freshness

`reviewed_at` is set on create, on every edit, and by `POST
/api/knowledge/pages/{id}/reviewed` ("still accurate"). Pages not reviewed for
`ARGUS_KNOWLEDGE_STALE_DAYS` (default 30) are flagged `stale` in search, claims,
and the dashboard and rank after fresh pages, but are never hidden: omission
would read as "nothing is known".

# 4. Open Questions

1. Page types and templates (runbook, decision record) once usage shows a need.

# 5. Constraints

- Everything in [single-node](single-node.spec.md): SQLite (FTS5 for search),
  no extra services.
- The FTS index is updated in the same transaction as every publish, restore,
  archive, and unarchive, so search never lags a committed page.
- A task spec states what the task needs; knowledge is background and is never
  the only place a requirement lives.

# 6. Implementation Consequences

- Migration 0007: `projects`, `tasks.project`, `task_knowledge` (pins),
  `knowledge_pages`, `knowledge_revisions`, `knowledge_fts`,
  `knowledge_proposals`; `decisions` rebuilt to allow the `knowledge` kind.
- `argus_api.knowledge.service` holds all page, search, and proposal logic;
  decisions call into it for proposals and saved answers.
- The dashboard renders Markdown with `react-markdown` + `remark-gfm`, never raw
  HTML, for pages, proposals, task specs, and results.
