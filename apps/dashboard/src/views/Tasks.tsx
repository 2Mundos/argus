import { type FormEvent, memo, type ReactNode, useCallback, useEffect, useState } from "react";
import { ActionButton, useAction } from "../ActionButton";
import {
  type ArgusEvent,
  api,
  boardTasks,
  call,
  ENDED_STATES,
  searchErrorMessage,
  type TaskBoard,
  type TaskKind,
  type TaskState,
  type TaskSummary,
} from "../api";
import { DateChip } from "../DateChip";
import { DELIVERABLES, type Deliverable } from "../deliverables";
import { reportPage, useLiveData } from "../live";
import { ProgressPie, TaskSignal } from "../milestones";
import { PageHeader } from "../PageHeader";
import { repositoryNeed, repositoryState, repositoryWarning } from "../projectRepository";
import { TaskLabel } from "../TaskLabel";
import {
  fetchTaskDefaults,
  sourceText,
  splitCapabilities,
  type TaskDefaultField,
  toText,
} from "../taskDefaults";
import { executorPin, harnessPin, parseBudget } from "../taskEdit";
import { NO_TASK_FILTER, type TaskFilter, tasksHash } from "../taskRoute";
import { boundedSnippet } from "../taskSearch";
import { WorkerChip } from "../WorkerChip";
import { AccountPin } from "./AccountPin";
import { useNow } from "./Activity";
import { ArchiveTasks } from "./ArchiveTasks";
import { ModelChip } from "./ModelChip";
import { ModelPin } from "./ModelPin";
import { ProjectRepositoryDialog } from "./ProjectRepositoryDialog";
import { TaskDetail } from "./TaskDetail";
import { NewTaskIntegrations } from "./TaskIntegrations";

const COLUMNS: { title: string; states: TaskState[] }[] = [
  { title: "Waiting", states: ["awaiting_approval", "pending", "blocked"] },
  { title: "Ready", states: ["ready"] },
  { title: "Running", states: ["claimed"] },
  { title: "In review", states: ["in_review"] },
  { title: "Done", states: ["succeeded"] },
  { title: "Failed / cancelled", states: ["failed", "cancelled"] },
];

type BoardSection =
  | { type: "column"; title: string; items: TaskSummary[] }
  | { type: "chip"; columns: { title: string; count: number | "…" }[] };

/** Empty columns and user-collapsed ones share a vertical chip stack. */
function boardSections(
  tasks: TaskSummary[] | undefined,
  collapsed: ReadonlySet<string>,
  loading: boolean,
  only: TaskState[] = [],
): BoardSection[] {
  const sections: BoardSection[] = [];
  // A state filter shows only the columns it can fill; the others were not requested.
  for (const column of COLUMNS) {
    if (only.length && !column.states.some((state) => only.includes(state))) continue;
    const items = (tasks ?? []).filter((t) => column.states.includes(t.state));
    const asChip = items.length === 0 || collapsed.has(column.title);
    if (asChip) {
      const last = sections[sections.length - 1];
      // Nothing is known before the first page, and ended tasks arrive after the active ones.
      const unknown =
        tasks === undefined ||
        (loading && column.states.every((state) => ENDED_STATES.includes(state)));
      const entry = { title: column.title, count: unknown ? ("…" as const) : items.length };
      if (last?.type === "chip") last.columns.push(entry);
      else sections.push({ type: "chip", columns: [entry] });
    } else {
      sections.push({ type: "column", title: column.title, items });
    }
  }
  return sections;
}

type BoardData = TaskBoard;

const boardEvent = (event: ArgusEvent) => event.subject_type === "task";

async function updateBoard(
  data: BoardData,
  events: ArgusEvent[],
  signal: AbortSignal,
  archived: boolean,
  states: TaskState[],
  q: string,
  project?: string,
): Promise<BoardData> {
  // Whether a changed task still matches is the server's call, so a search refetches.
  if (q || project) return boardTasks(archived, { signal, states, q, project });
  const ids = [...new Set(events.map((event) => event.subject_id))];
  const known = new Set(data.items.map((task) => task.id));
  if (ids.some((id) => !known.has(id))) return boardTasks(archived, { signal, states, project });
  const batches = [];
  for (let i = 0; i < ids.length; i += 50) {
    batches.push(
      call(
        api.GET("/api/tasks", {
          params: { query: { ids: ids.slice(i, i + 50).join(",") } },
          signal,
        }),
      ),
    );
  }
  const pages = await Promise.all(batches);
  // The default view is summary; only view=compact rows lack these fields.
  const changed = pages.flatMap((page) => page.items as TaskSummary[]);
  if (
    changed.length !== ids.length ||
    changed.some((task) => Boolean(task.archived_at) !== archived) ||
    (states.length > 0 && changed.some((task) => !states.includes(task.state))) ||
    (project && changed.some((task) => task.project !== project))
  ) {
    return boardTasks(archived, { signal, states, project });
  }
  const replacements = new Map(changed.map((task) => [task.id, task]));
  return { ...data, items: data.items.map((task) => replacements.get(task.id) ?? task) };
}

export function Tasks({
  filter = NO_TASK_FILTER,
  project,
  showHeader = true,
}: {
  filter?: TaskFilter;
  project?: string;
  showHeader?: boolean;
}) {
  const { states, q } = filter;
  const filterKey = states.join(",");
  // `#tasks/<id>` (linked from Fleet) opens that task's detail.
  const [selected, setSelected] = useState<string | null>(
    () => window.location.hash.match(/^#tasks\/(.+)$/)?.[1] ?? null,
  );
  const [creating, setCreating] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const [archived, setArchived] = useState(false);
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => new Set());
  // The submitted search lives in the URL filter; the text being typed is local.
  const [draft, setDraft] = useState(q);
  useEffect(() => setDraft(q), [q]);
  const { data, error, reload } = useLiveData(
    (signal) =>
      boardTasks(archived, {
        signal,
        states,
        project,
        q: q || undefined,
        onPage: (page) => reportPage(signal, page),
      }),
    [archived, filterKey, q, project],
    {
      // Task events carry no project field, so a project-scoped board refetches on
      // any task event; updateBoard decides whether the change still matches.
      accepts: boardEvent,
      update: (data, events, signal) =>
        updateBoard(data, events, signal, archived, states, q, project),
    },
  );
  function search(e: FormEvent) {
    e.preventDefault();
    const text = draft.trim();
    if (text === q) return reload();
    // The hashchange handler applies it; a new `q` drops the old results and cursors.
    window.location.hash = tasksHash({ states, q: text });
  }
  const failure = error ? searchErrorMessage(error) : undefined;
  const tasks = data?.items;
  const now = useNow(30_000);
  const openTask = useCallback((id: string) => setSelected(id), []);

  function collapseColumn(title: string) {
    setCollapsed((prev) => new Set(prev).add(title));
  }
  function expandColumn(title: string) {
    setCollapsed((prev) => {
      const next = new Set(prev);
      next.delete(title);
      return next;
    });
  }

  if (selected) {
    return (
      <section className="task-panel-page">
        <TaskDetail
          taskId={selected}
          variant="panel"
          onClose={() => {
            setSelected(null);
            if (window.location.hash.startsWith("#tasks/"))
              window.location.hash = tasksHash(filter);
          }}
        />
      </section>
    );
  }

  return (
    <section className={selected ? "tasks-open" : "tasks-page"}>
      {showHeader ? (
        <PageHeader title="Tasks">
          <label className="check header-toggle">
            <input
              type="checkbox"
              checked={archived}
              onChange={(e) => setArchived(e.target.checked)}
            />
            Archived
          </label>
          {!archived && (
            <button type="button" className="compact" onClick={() => setArchiving(true)}>
              Archive…
            </button>
          )}
          <button type="button" className="primary compact" onClick={() => setCreating(true)}>
            + New task
          </button>
        </PageHeader>
      ) : (
        <div className="tasks-embedded-actions">
          <div className="actions left">
            <label className="check header-toggle">
              <input
                type="checkbox"
                checked={archived}
                onChange={(e) => setArchived(e.target.checked)}
              />
              Archived
            </label>
            {!archived && (
              <button type="button" className="compact" onClick={() => setArchiving(true)}>
                Archive…
              </button>
            )}
            <button type="button" className="primary compact" onClick={() => setCreating(true)}>
              + New task
            </button>
          </div>
        </div>
      )}
      {states.length > 0 && (
        <p className="muted small" data-testid="task-filter">
          Filtered by state:{" "}
          <span className="badge">{states.map((s) => s.replace("_", " ")).join(", ")}</span>{" "}
          <a href={tasksHash({ q })}>Clear</a>
        </p>
      )}
      <form role="search" className="task-search" onSubmit={search}>
        <label htmlFor="task-search-input">Search tasks</label>
        <input
          id="task-search-input"
          type="search"
          value={draft}
          placeholder="Words in a title or spec"
          onChange={(e) => setDraft(e.target.value)}
        />
        <button type="submit" className="compact">
          Search
        </button>
        {(q || draft) && (
          <button
            type="button"
            className="compact"
            onClick={() => {
              setDraft("");
              window.location.hash = tasksHash({ states });
            }}
          >
            Clear
          </button>
        )}
      </form>
      {archived && <p className="muted small">Showing archived tasks only.</p>}
      {q && (
        <p className="muted small">
          Literal text match on title and spec for “{q}”; it does not say a task covers the same
          work.
        </p>
      )}
      {failure && (
        <p className="error" role="alert">
          {failure.text}
        </p>
      )}
      {q && !error && data && !data.loading && data.items.length === 0 && (
        <p className="muted" role="status">
          No tasks match “{q}”.
        </p>
      )}
      {data && !data.loading && data.total > data.items.length && (
        <p className="muted small">
          Showing the newest {data.items.length} of {data.total}
          {states.length > 0 || q ? " matching" : ""} tasks.
        </p>
      )}
      {!(q && (error || (data && data.items.length === 0))) && (
        <div className="board">
          {boardSections(tasks, collapsed, data?.loading === true, states).map((section) =>
            section.type === "chip" ? (
              <div
                key={section.columns.map((c) => c.title).join("|")}
                className="column-empty-stack"
              >
                {section.columns.map((column) => (
                  <div
                    key={column.title}
                    className="column-chip"
                    aria-label={`${column.title}: ${column.count}`}
                  >
                    <span className="column-chip-label">
                      {column.title} <span className="count">{column.count}</span>
                    </span>
                    {typeof column.count === "number" && column.count > 0 && (
                      <button
                        type="button"
                        className="column-toggle"
                        aria-label={`Expand ${column.title}`}
                        onClick={() => expandColumn(column.title)}
                      >
                        ▸
                      </button>
                    )}
                  </div>
                ))}
              </div>
            ) : (
              <div className="column" key={section.title}>
                <div className="column-head">
                  <h3>
                    {section.title} <span className="count">{section.items.length}</span>
                  </h3>
                  <button
                    type="button"
                    className="column-toggle"
                    aria-label={`Collapse ${section.title}`}
                    onClick={() => collapseColumn(section.title)}
                  >
                    ▾
                  </button>
                </div>
                <div className="column-body">
                  {section.items.map((task) => (
                    <TaskCard
                      key={task.id}
                      task={task}
                      onOpen={openTask}
                      now={task.state === "claimed" ? now : 0}
                    />
                  ))}
                </div>
              </div>
            ),
          )}
        </div>
      )}
      {archiving && <ArchiveTasks onClose={() => setArchiving(false)} />}
      {creating && <NewTask onClose={() => setCreating(false)} initialProject={project} />}
    </section>
  );
}

const TaskCard = memo(
  function TaskCard({
    task,
    onOpen,
    now,
  }: {
    task: TaskSummary;
    onOpen: (id: string) => void;
    now: number;
  }) {
    return (
      <button type="button" className="task-card has-pie" onClick={() => onOpen(task.id)}>
        <ProgressPie percent={task.state === "succeeded" ? 100 : (task.progress_percent ?? 0)} />
        <TaskLabel className="title" title={task.title} project={task.project} />
        <span className="meta">
          <span className={`badge state-${task.state}`}>{task.state.replace("_", " ")}</span>
          {task.kind !== "generic" && <span className="badge">{task.kind}</span>}
          {task.priority !== 0 && <span className="badge">p{task.priority}</span>}
          {task.attempt > 0 && <span className="muted">attempt {task.attempt}</span>}
        </span>
        {task.models?.used?.length ? (
          <span className="meta" aria-label="Models used">
            {task.models.used.map((item) => (
              <ModelChip
                key={`${item.layer}-${item.tool}-${item.model}`}
                tool={item.tool ?? item.layer}
                model={item.model}
                observed={item.observed}
              />
            ))}
          </span>
        ) : null}
        {task.state === "claimed" && task.assignee_agent_id && (
          <span className="muted small">
            <WorkerChip agentId={task.assignee_agent_id} layout="inline" /> · lease{" "}
            <DateChip at={task.lease_expires_at} now={now} />
          </span>
        )}
        {task.state === "claimed" && <TaskSignal task={task} now={now} />}
        {task.block_reason && task.state === "blocked" && (
          <span className="small muted">{task.block_reason}</span>
        )}
        {task.matches?.map((hit) => (
          <span key={hit.field} className="small muted match-snippet">
            {hit.field}: {boundedSnippet(hit.snippet)}
          </span>
        ))}
      </button>
    );
  },
  (a, b) =>
    a.onOpen === b.onOpen &&
    a.now === b.now &&
    (a.task === b.task ||
      JSON.stringify(cardFields(a.task)) === JSON.stringify(cardFields(b.task))),
);

function cardFields(task: TaskSummary) {
  return [
    task.id,
    task.title,
    task.project,
    task.state,
    task.kind,
    task.priority,
    task.attempt,
    task.assignee_agent_id,
    task.lease_expires_at,
    task.block_reason,
    task.models?.used,
    task.progress_message,
    task.progress_percent,
    task.progress_at,
    task.progress_kind,
    task.progress_data,
    task.output_at,
    task.matches,
  ];
}

function NewTask({ onClose, initialProject }: { onClose: () => void; initialProject?: string }) {
  const { data: kinds } = useLiveData<TaskKind[]>(
    (signal) => call(api.GET("/api/task-kinds", { signal })),
    [],
  );
  const { data: projects } = useLiveData(
    (signal) => call(api.GET("/api/projects", { signal })),
    [],
  );
  const {
    data: stats,
    error: statsError,
    reload: reloadStats,
  } = useLiveData((signal) => call(api.GET("/api/projects/stats", { signal })), []);
  const [form, setForm] = useState({
    title: "",
    spec: "",
    kind: "generic",
    project: initialProject ?? "",
    accountProviders: "",
    requires_approval: false,
    review: "none" as "none" | "human" | "orchestrator",
    harnessModel: "",
    executorModel: "",
    harnessAccount: "",
    // null until changed: the field shows the effective task default, which the server
    // applies to a field the request leaves out (task-defaults.spec.md 3.3).
    deliverable: null as Deliverable | null,
    priority: null as string | null,
    difficulty: null as "" | "trivial" | "simple" | "standard" | "hard" | null,
    capabilities: null as string | null,
    budget: null as string | null,
    maxAttempts: null as string | null,
    timeoutMinutes: null as string | null,
    // Integration ids, kept with the project they were chosen in.
    integrations: { project: "", ids: [] as string[] },
  });
  const [error, setError] = useState<string>();
  const [configuring, setConfiguring] = useState<string | null>(null);
  // A repository saved from this dialog counts at once, but only until the stats are
  // re-read: the override belongs to the stats object it was saved against.
  const [saved, setSaved] = useState<Record<string, { url: string | null; stats: unknown }>>({});
  const action = useAction();
  const set = <K extends keyof typeof form>(key: K, value: (typeof form)[K]) =>
    setForm((f) => ({ ...f, [key]: value }));
  // `global` scopes knowledge and settings only; a task needs a real project.
  const taskProjects = (projects ?? []).filter((p) => p.slug !== "global");
  const project = form.project || taskProjects[0]?.slug || "";
  // What Argus knows about the selected project's repository: "unknown" until the stats
  // answer (or if they fail), never assumed missing.
  const override = saved[project]?.stats === stats ? saved[project] : undefined;
  const state = override
    ? override.url
      ? "set"
      : "absent"
    : repositoryState(stats?.find((s) => s.slug === project));
  // What the server would give each field left out, for this project and kind.
  const { data: defaults, error: defaultsError } = useLiveData(
    (signal) =>
      project ? fetchTaskDefaults(project, form.kind, signal) : Promise.resolve(undefined),
    [project, form.kind],
  );
  const shown = defaults?.values;
  const integrations = form.integrations.project === project ? form.integrations.ids : [];
  const deliverable = form.deliverable ?? shown?.deliverable ?? "response";
  const capabilitiesText =
    form.capabilities ??
    toText(
      "required_capabilities",
      shown?.required_capabilities ??
        kinds?.find((k) => k.name === form.kind)?.default_capabilities ??
        [],
    );
  const need = repositoryNeed(deliverable, splitCapabilities(capabilitiesText));
  const timeout = form.timeoutMinutes ?? (shown?.timeout_s ? String(shown.timeout_s / 60) : "");
  // A refusal the defaults would cause; a changed deliverable or capability list is
  // the creator's own choice, which the server checks on create.
  const problems =
    form.deliverable === null && form.capabilities === null
      ? (defaults?.problems ?? []).filter(
          (p) =>
            p.code !== "deliverable_repository" ||
            !(state === "absent" || state === "invalid" || state === "conflict"),
        )
      : [];
  const hint = (field: TaskDefaultField, key: keyof typeof form) => {
    const id = `new-task-${field}-default`;
    const changed = form[key] !== null;
    return {
      describedBy: id,
      node: (
        <span className="muted small field-hint" id={id}>
          {changed ? (
            <>
              changed{" "}
              <button type="button" className="link" onClick={() => set(key, null as never)}>
                Use default
              </button>
            </>
          ) : defaults ? (
            `default, ${sourceText(defaults.sources[field])}`
          ) : (
            "default"
          )}
        </span>
      ),
    };
  };
  const field = (
    name: TaskDefaultField,
    key: keyof typeof form,
    label: ReactNode,
    control: (describedBy: string) => ReactNode,
  ) => {
    const h = hint(name, key);
    return (
      <div className="field">
        <label>
          {label}
          {control(h.describedBy)}
        </label>
        {h.node}
      </div>
    );
  };

  async function submit(e: FormEvent) {
    e.preventDefault();
    await action.run(async () => {
      try {
        const accountProviders = form.accountProviders
          .split(",")
          .map((c) => c.trim())
          .filter(Boolean);
        // Only changed fields are sent: the server applies the default the others show.
        const parsed = form.budget === null ? null : parseBudget(form.budget);
        if (parsed && "error" in parsed) return setError(parsed.error);
        const whole = (text: string | null, what: string) => {
          if (text === null) return undefined;
          if (!/^-?\d+$/.test(text.trim())) throw new Error(`${what} is a whole number.`);
          return Number(text);
        };
        const priority = whole(form.priority, "Priority");
        const maxAttempts = whole(form.maxAttempts, "Max attempts");
        const minutes = form.timeoutMinutes?.trim();
        const harness = harnessPin(form.harnessModel);
        const executor = executorPin(form.executorModel);
        await call(
          api.POST("/api/tasks", {
            body: {
              title: form.title,
              spec: form.spec,
              kind: form.kind,
              project,
              requires_approval: form.requires_approval,
              review: form.review,
              ...(form.deliverable !== null ? { deliverable: form.deliverable } : {}),
              ...(priority !== undefined ? { priority } : {}),
              ...(maxAttempts !== undefined ? { max_attempts: maxAttempts } : {}),
              // An emptied difficulty asks for inference; an emptied timeout for the
              // kind's, then the server's; an emptied list for no capabilities.
              ...(form.difficulty !== null ? { difficulty: form.difficulty || null } : {}),
              ...(minutes !== undefined
                ? { timeout_s: minutes ? Math.round(Number(minutes) * 60) : null }
                : {}),
              ...(form.capabilities !== null
                ? { required_capabilities: splitCapabilities(form.capabilities) }
                : {}),
              ...(accountProviders.length ? { account_providers: accountProviders } : {}),
              ...(harness ? { harness_model: harness } : {}),
              ...(executor ? { executor_model: executor } : {}),
              ...(form.harnessAccount ? { harness_account: form.harnessAccount } : {}),
              ...(parsed ? { budget_usd: parsed.value } : {}),
              ...(integrations.length ? { integrations } : {}),
            },
          }),
        );
        onClose();
      } catch (err) {
        setError((err as Error).message);
      }
    });
  }

  return (
    <>
      <div className="overlay">
        <form className="dialog" onSubmit={submit}>
          <h2>New task</h2>
          <label>
            Title
            <input value={form.title} onChange={(e) => set("title", e.target.value)} required />
          </label>
          <label>
            Spec (Markdown)
            <textarea rows={8} value={form.spec} onChange={(e) => set("spec", e.target.value)} />
          </label>
          <div className="row">
            <label>
              Kind
              <select value={form.kind} onChange={(e) => set("kind", e.target.value)}>
                {(kinds ?? [{ name: "generic" }]).map((k) => (
                  <option key={k.name}>{k.name}</option>
                ))}
              </select>
            </label>
            <label>
              Project
              <select value={project} onChange={(e) => set("project", e.target.value)} required>
                {taskProjects.map((p) => (
                  <option key={p.slug} value={p.slug}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
            {field("deliverable", "deliverable", "Deliverable", (d) => (
              <select
                value={deliverable}
                aria-describedby={d}
                onChange={(e) => set("deliverable", e.target.value as Deliverable)}
              >
                {DELIVERABLES.map((x) => (
                  <option key={x.value} value={x.value}>
                    {x.label}
                  </option>
                ))}
              </select>
            ))}
            {field("priority", "priority", "Priority", (d) => (
              <input
                type="number"
                step={1}
                value={form.priority ?? String(shown?.priority ?? 0)}
                aria-describedby={d}
                onChange={(e) => set("priority", e.target.value)}
              />
            ))}
            {field("max_attempts", "maxAttempts", "Max attempts", (d) => (
              <input
                type="number"
                min={1}
                max={20}
                step={1}
                value={form.maxAttempts ?? String(shown?.max_attempts ?? 3)}
                aria-describedby={d}
                onChange={(e) => set("maxAttempts", e.target.value)}
              />
            ))}
            <label>
              Review
              <select
                value={form.review}
                onChange={(e) => set("review", e.target.value as typeof form.review)}
              >
                <option value="none">none</option>
                <option value="human">human</option>
                <option value="orchestrator">orchestrator</option>
              </select>
            </label>
          </div>
          {project && (state === "absent" || state === "invalid" || state === "conflict") && (
            <div className="repo-notice" role="status">
              <p className="small">{repositoryWarning(project, state, need)}</p>
              <button type="button" disabled={action.busy} onClick={() => setConfiguring(project)}>
                Set repository…
              </button>
            </div>
          )}
          {project && state === "unknown" && statsError && (
            <p className="muted small" role="status">
              Could not check whether {project} has a repository ({statsError.message}).{" "}
              <button type="button" className="link" onClick={reloadStats}>
                Retry
              </button>
            </p>
          )}
          {problems.map((p) => (
            <p key={p.code} className="error small">
              With the defaults shown, Argus refuses this task: {p.message}
            </p>
          ))}
          {defaultsError && (
            <p className="muted small">
              Could not read the task defaults ({defaultsError.message}); fields left as they are
              still get the server's defaults.
            </p>
          )}
          {field(
            "difficulty",
            "difficulty",
            "Difficulty (trivial and simple work goes to a cheap executor; hard work to the strongest)",
            (d) => (
              <select
                value={form.difficulty ?? shown?.difficulty ?? ""}
                aria-describedby={d}
                onChange={(e) =>
                  set("difficulty", e.target.value as NonNullable<typeof form.difficulty>)
                }
              >
                <option value="">decide from the title and spec</option>
                <option value="trivial">trivial</option>
                <option value="simple">simple</option>
                <option value="standard">standard</option>
                <option value="hard">hard</option>
              </select>
            ),
          )}
          {field(
            "required_capabilities",
            "capabilities",
            "Required capabilities (comma-separated; empty: none)",
            (d) => (
              <input
                value={capabilitiesText}
                aria-describedby={d}
                onChange={(e) => set("capabilities", e.target.value)}
              />
            ),
          )}
          <label>
            Restrict to account providers (comma-separated, e.g. local; empty: any)
            <input
              value={form.accountProviders}
              onChange={(e) => set("accountProviders", e.target.value)}
            />
          </label>
          <label>
            Harness engine and model (empty: the model policy)
            <ModelPin
              field="harness_model"
              id="new-harness-model"
              value={form.harnessModel}
              onChange={(v) => set("harnessModel", v)}
            />
          </label>
          <label>
            Harness account (empty: any; local and custom accounts bring their own model)
            <AccountPin
              id="new-harness-account"
              value={form.harnessAccount}
              onChange={(v) => set("harnessAccount", v)}
            />
          </label>
          <label>
            Coding executor and model (empty: the model policy)
            <ModelPin
              field="executor_model"
              id="new-executor-model"
              value={form.executorModel}
              onChange={(v) => set("executorModel", v)}
            />
          </label>
          {field("budget_usd", "budget", "Budget (USD, estimated; empty: no budget)", (d) => (
            <input
              type="number"
              min={0}
              step="any"
              value={form.budget ?? toText("budget_usd", shown?.budget_usd)}
              aria-describedby={d}
              onChange={(e) => set("budget", e.target.value)}
            />
          ))}
          {field(
            "timeout_s",
            "timeoutMinutes",
            "Timeout in minutes (empty: the kind's, then the server's default)",
            (d) => (
              <input
                type="number"
                min={1}
                value={timeout}
                aria-describedby={d}
                onChange={(e) => set("timeoutMinutes", e.target.value)}
              />
            ),
          )}
          <NewTaskIntegrations
            project={project}
            chosen={integrations}
            onChange={(ids) => set("integrations", { project, ids })}
          />
          <label className="check">
            <input
              type="checkbox"
              checked={form.requires_approval}
              onChange={(e) => set("requires_approval", e.target.checked)}
            />
            Requires approval before it can start
          </label>
          {error && <p className="error">{error}</p>}
          <div className="actions">
            <button type="button" disabled={action.busy} onClick={onClose}>
              Cancel
            </button>
            <ActionButton type="submit" className="primary" pending={action.busy}>
              Create
            </ActionButton>
          </div>
        </form>
      </div>
      {configuring && (
        <ProjectRepositoryDialog
          slug={configuring}
          onCancel={() => setConfiguring(null)}
          onSaved={(p) => {
            setSaved((s) => ({ ...s, [p.slug]: { url: p.repository_url ?? null, stats } }));
            setConfiguring(null);
            reloadStats();
          }}
        />
      )}
    </>
  );
}
