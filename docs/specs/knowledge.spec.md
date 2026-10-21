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

`projects` (`slug`, `name`, `description`) are a first-class scope, managed by
orchestrators and admins (`PUT /api/projects/{slug}`). `global` exists by
default for fleet-wide knowledge. **Every task belongs to a project**: it is
required on creation, except that a subtask without one inherits its parent's
(`422 project_required` otherwise). No per-project permissions: any valid token
reads every project.

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
