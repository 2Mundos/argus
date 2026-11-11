---
status: implemented
created: 2026-11-06
updated: 2026-11-11
---

# Task defaults

# 1. Purpose and Scope

A new task's deliverable, priority, difficulty, required capabilities, budget,
attempt limit and timeout are chosen by whoever creates it. Clients filled the
fields they left out with fixed values (`response`, `0`, `[]`, `3`), so an
operator could not say "tasks in this project open and merge a PR" once and have
every entry point honour it. This spec adds **task defaults**: typed
[settings](settings.spec.md) with a global value and per-project overrides that
fill, at the API, the fields a creator leaves out.

The owner's case: the `argus` project defaults to `deliverable: pr_merged` with
`required_capabilities: ["repository:write"]`; a task created there without a
deliverable is held to a merged PR, one that names `response` is not, and other
projects are unaffected.

# 2. Non-Goals

- No model, account, provider or credential defaults; pins stay per task
  ([task models](task-models.spec.md)).
- No change to an existing task. Defaults are read once, at creation; editing a
  setting never rewrites a stored task, and [task edit](task-edit.spec.md) does
  not re-apply them.
- A default deliverable is a requested outcome, not an authorization. It grants no
  capability, does not allow a worker to merge (that is `delivery.auto_merge` or
  the task input, [settings](settings.spec.md) 3.10) and does not waive acceptance
  or review (3.11, 3.5 there).
- No project hierarchy: a project has no parent project. "Inheritance" is a
  subtask taking its parent task's project, then that project's defaults.
- No second budget setting: the budget default is the existing
  `task_default_budget_usd` ([task cost](task-cost.spec.md) 3.4).

# 3. Decisions Made

## 3.1 Keys

One settings key per field, scopes `global` and `project`, so each field is
inherited, overridden and reset on its own (a partial project override inherits
the other fields from global):

| Key | Value | Unset (registry default) |
| --- | --- | --- |
| `task_default_deliverable` | a [deliverable](task-deliverable.spec.md) | `response` |
| `task_default_priority` | integer, -1000000..1000000 | `0` |
| `task_default_difficulty` | `trivial` \| `simple` \| `standard` \| `hard` \| `null` | `null` (infer) |
| `task_default_required_capabilities` | list of strings (unique, ≤ 50, 1-200 chars) | `[]` |
| `task_default_budget_usd` | number `> 0`, `≤ 10000`, or `null` | `null` (no budget) |
| `task_default_max_attempts` | integer 1..20 | `3` |
| `task_default_timeout_s` | integer 60..604800, or `null` | `null` (fallback) |

Storage, validation, permissions (admin writes, any token reads), the
`settings.changed` audit event, `source` reporting and `DELETE` to reset are those
of every setting ([settings](settings.spec.md) 3.2, 3.3). A stored value is an
override: `[]` for capabilities, `0` for priority and `null` for budget are values,
distinct from "no row" (inherit). A stored `null` for difficulty or timeout means
"use the fallback below", so a project can opt out of a global default.

## 3.2 Precedence

For each field, independently, a new task gets the first of:

1. the value the creator sent (explicit);
2. the project's override (`source: project`);
3. the global value (`source: global`);
4. the kind's default ([kinds](worker-lifecycle.spec.md): `default_capabilities`,
   `default_max_attempts`, `default_timeout_s`), for those three fields;
5. the built-in fallback: `response`, `0`, `[]`, no budget, `3`; difficulty is
   inferred from the title and spec ([task difficulty](task-difficulty.spec.md));
   timeout is left unset, so the server's `ARGUS_TASK_TIMEOUT_S` applies at claim.

A stored `null` difficulty or timeout (step 2 or 3) skips to steps 4-5; a stored
`null` budget is final ("no budget").

**Omitted vs `null` in a request.** For `deliverable`, `priority`,
`required_capabilities` and `max_attempts`, an omitted field and an explicit
`null` both take the default. For `budget_usd`, `timeout_s` and `difficulty`, an
explicit `null` is a value: no budget, the kind/server timeout, inferred
difficulty. `required_capabilities: []` is explicit and replaces every default.
Request schemas (`TaskCreate`, draft bodies, MCP tools) carry no default for these
fields, so no client can bake one in.

**Project first.** The project is resolved before the defaults: a subtask without
a project takes its parent's, then that project's defaults. A difficulty taken
from a setting is recorded with `difficulty_source: declared` (an operator chose
it); an inferred one stays `inferred`.

## 3.3 Snapshot

The resolved values are stored in the task's own columns in the creation
transaction; nothing reads the settings again for that task. `task.created`
records `defaults: {field: source}` for every field that did not come from the
request (`project`, `global`, `kind`, `built_in`, `inferred`), so the audit trail
shows where each value came from. A creation retried with the same
`Idempotency-Key` returns the stored task even if a setting changed in between.

## 3.4 Entry points

Every creation goes through `tasks.service.create_task`, which applies 3.2:

- `POST /api/tasks` and MCP `create_task` (orchestrators, admins, worker subtasks).
- Draft publish ([task drafts](task-drafts.spec.md)): a draft stores only the
  fields its author set, so defaults are resolved at **publish**, for the draft's
  project at that moment. Changing a draft's project or a setting before publish
  changes what it gets; an explicit draft field always wins. A replayed publish
  returns its stored answer and creates nothing.
- [Routine](routines.spec.md) runs: a template keeps only the fields it names;
  each run takes the defaults current at that run for the others. A template may
  name `deliverable`, `max_attempts` and `timeout_s` (as well as `priority`,
  `required_capabilities`, `difficulty`, `budget_usd`) to pin them. A template save
  is checked against the defaults current then.
- Tasks Argus creates for itself keep their fixed shape and set it explicitly:
  the independent `code_review` child (`deliverable: response`, no capability),
  the project checkout task (`response`, no capability) and routine findings
  (`response`, no capability; their content is untrusted, so a default must not
  give them `repository:write`). Their priority, attempts, timeout and budget
  follow 3.2.

## 3.5 Code deliverables

A default `branch` or `pr_*` deliverable obeys the creation rules of [task
deliverable](task-deliverable.spec.md) 3.3 like an explicit one: the task needs
`repository:write` among its resolved capabilities and a project with a
repository. Argus never adds the capability: the operator sets
`task_default_required_capabilities` (or the creator sends it). When the
deliverable or the capabilities came from a default, the `422
deliverable_capability` or `deliverable_repository` error names the setting and its
scope in `details` and says what to change. A creator who sends
`required_capabilities: []` against a `pr_merged` default gets that error, not a
silently widened task.

## 3.6 Effective defaults

`GET /api/projects/{slug}/task-defaults?kind=generic` (any token that may read the
project, as for its settings) returns what a task created there now would get for
every omitted field:

```json
{
  "project": "argus",
  "kind": "generic",
  "values": {"deliverable": "pr_merged", "priority": 0, "difficulty": null,
             "required_capabilities": ["repository:write"], "budget_usd": null,
             "max_attempts": 3, "timeout_s": null},
  "sources": {"deliverable": "project", "priority": "built_in",
              "difficulty": "inferred", "required_capabilities": "project",
              "budget_usd": "built_in", "max_attempts": "built_in",
              "timeout_s": "built_in"},
  "problems": []
}
```

`problems` lists `{code, field, message}` for a combination creation would refuse
(`deliverable_capability`, `deliverable_repository`), so the settings screen and the
create form can warn before anything is created. Writing a setting is not refused
for such a combination: the operator sets the deliverable and the capabilities in
two writes. The per-key `source` of the stored values stays in `GET
/api/projects/{slug}/settings` and `GET /api/settings`. MCP reads them with
`get_settings {project}`; no new tool.

## 3.7 Dashboard

- **Settings → Task defaults** (`#setup/settings`, or `#setup/settings/<slug>` for
  a project's scope): one control per field, "Built-in" or "Set" with a typed
  input checked as the registry checks it, the effective value and its source, and
  the `problems` of 3.6 beside the fields they name.
- **Projects** is a project workspace. `#projects` lists the projects, each name
  linking to `#projects/<slug>`; a project page has a project selector and the tabs
  **Overview** (repository, its editing and remediation, task counts, Git author)
  **Graph** (`#projects/<slug>/graph`, the read-only [task graph](task-graph.spec.md))
  and **Task defaults** (`#projects/<slug>/task-defaults`), plus a link to the
  project's other settings. The selection lives in the URL, so reload and shared
  links keep it. Runtime images have no dashboard page
  ([project runtime](project-runtime.spec.md)).
- Project **Task defaults**: per field, Inherit (naming the global or built-in value
  it takes) or Override, the effective value and its source (project, global, the
  kind, built-in, inferred). Choosing Inherit and saving deletes the override. An
  empty capability list, priority `0`, no budget and the difficulty or timeout
  fallback are overrides, distinct from Inherit. Fields save one by one; a refused
  one keeps its edit and says why while the others save. After a save, a switch of
  project or a reload the form shows the saved values.
- **Unsaved edits**: leaving a dirty form by any hash change (menu, tab, project
  selector, back) or opening a worker asks first and stays on "no"; reloading or
  closing the page gets the browser's prompt.
- **New task** prefills each defaultable field from the effective defaults of the
  chosen project and kind, labels where each comes from, re-reads them when either
  changes, and shows the problems the defaults would cause. It sends only the
  fields the user changed ("Use default" takes a change back); an emptied field
  sends its explicit empty value (no capabilities, inference, no timeout, no
  budget).

# 4. Open Questions

- Should a kind's own default rank above the project's? Today a project default
  wins (3.2), which is what an operator setting a project-wide timeout expects;
  a kind that must never take repository capability sets its own explicit
  defaults on the tasks Argus creates (3.4).
- Should an orchestrator be allowed to write project task defaults? Admin only,
  like every setting ([settings](settings.spec.md) 4).

# 5. Constraints

- No migration: the values live in `settings` and the existing task columns.
- Resolution is one function used by create, draft publish, routine runs and the
  effective-defaults read, so they cannot disagree.
- Settings are read inside the creation write transaction, so a concurrent setting
  change is seen entirely or not at all.

# 6. Implementation Consequences

- `settings/registry.py`: the six new keys; `task_default_budget_usd` unchanged.
- `tasks/defaults.py`: resolution (3.2), sources, the problems of 3.6, and the
  annotated deliverable errors of 3.5; `tasks/service.py` resolves the project
  first, then the defaults, and records the sources on `task.created`.
- `argus_core.tasks.TaskCreate`: `deliverable`, `priority`,
  `required_capabilities` and `max_attempts` become optional with no default.
- MCP `create_task`: `priority` is omitted unless given; the description says
  omitted fields take the project's task defaults.
- Routine templates accept `deliverable`, `max_attempts` and `timeout_s`.
- OpenAPI and the dashboard client are regenerated.
- Dashboard: `taskDefaults.ts` (fields, parsing, labels), `TaskDefaultsEditor`
  (Settings and the project tab), the `Projects` workspace, the New task form in
  `Tasks.tsx`, and `unsaved.ts` for the leave guard.
