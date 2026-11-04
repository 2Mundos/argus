import { type FormEvent, memo, useCallback, useState } from "react";
import { ActionButton, useAction } from "../ActionButton";
import {
  type ArgusEvent,
  api,
  call,
  newestTasks,
  type TaskKind,
  type TaskState,
  type TaskSummary,
} from "../api";
import { DateChip } from "../DateChip";
import { DELIVERABLES, type Deliverable } from "../deliverables";
import { reportPage, useLiveData } from "../live";
import { TaskSignal } from "../milestones";
import { PageHeader } from "../PageHeader";
import { repositoryNeed, repositoryState, repositoryWarning } from "../projectRepository";
import { TaskLabel } from "../TaskLabel";
import { executorPin, harnessPin } from "../taskEdit";
import { WorkerChip } from "../WorkerChip";
import { AccountPin } from "./AccountPin";
import { useNow } from "./Activity";
import { ArchiveTasks } from "./ArchiveTasks";
import { ModelChip } from "./ModelChip";
import { ModelPin } from "./ModelPin";
import { ProjectRepositoryDialog } from "./ProjectRepositoryDialog";
import { TaskDetail } from "./TaskDetail";

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
  | { type: "chip"; columns: { title: string; count: number }[] };

/** Empty columns and user-collapsed ones share a vertical chip stack. */
function boardSections(
  tasks: TaskSummary[] | undefined,
  collapsed: ReadonlySet<string>,
): BoardSection[] {
  const sections: BoardSection[] = [];
  for (const column of COLUMNS) {
    const items = (tasks ?? []).filter((t) => column.states.includes(t.state));
    const asChip = items.length === 0 || collapsed.has(column.title);
    if (asChip) {
      const last = sections[sections.length - 1];
      const entry = { title: column.title, count: items.length };
      if (last?.type === "chip") last.columns.push(entry);
      else sections.push({ type: "chip", columns: [entry] });
    } else {
      sections.push({ type: "column", title: column.title, items });
    }
  }
  return sections;
}

type BoardData = { items: TaskSummary[]; total: number };

const boardEvent = (event: ArgusEvent) => event.subject_type === "task";

async function updateBoard(
  data: BoardData,
  events: ArgusEvent[],
  signal: AbortSignal,
  archived: boolean,
): Promise<BoardData> {
  const ids = [...new Set(events.map((event) => event.subject_id))];
  const known = new Set(data.items.map((task) => task.id));
  if (ids.some((id) => !known.has(id))) return newestTasks(5, archived, { signal });
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
  const changed = pages.flatMap((page) => page.items);
  if (
    changed.length !== ids.length ||
    changed.some((task) => Boolean(task.archived_at) !== archived)
  ) {
    return newestTasks(5, archived, { signal });
  }
  const replacements = new Map(changed.map((task) => [task.id, task]));
  return { ...data, items: data.items.map((task) => replacements.get(task.id) ?? task) };
}

export function Tasks() {
  // `#tasks/<id>` (linked from Fleet) opens that task's detail.
  const [selected, setSelected] = useState<string | null>(
    () => window.location.hash.match(/^#tasks\/(.+)$/)?.[1] ?? null,
  );
  const [creating, setCreating] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const [archived, setArchived] = useState(false);
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => new Set());
  const { data, error } = useLiveData(
    (signal) => newestTasks(5, archived, { signal, onPage: (page) => reportPage(signal, page) }),
    [archived],
    {
      accepts: boardEvent,
      update: (data, events, signal) => updateBoard(data, events, signal, archived),
    },
  );
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
            if (window.location.hash.startsWith("#tasks/")) window.location.hash = "tasks";
          }}
        />
      </section>
    );
  }

  return (
    <section className={selected ? "tasks-open" : "tasks-page"}>
      <PageHeader title="Tasks" lead="Work waiting, running and finished.">
        <label className="check">
          <input
            type="checkbox"
            checked={archived}
            onChange={(e) => setArchived(e.target.checked)}
          />
          Archived
        </label>
        {!archived && (
          <button type="button" onClick={() => setArchiving(true)}>
            Archive…
          </button>
        )}
        <button type="button" className="primary" onClick={() => setCreating(true)}>
          New task
        </button>
      </PageHeader>
      {archived && <p className="muted small">Showing archived tasks only.</p>}
      {error && <p className="error">{error.message}</p>}
      {data && data.total > data.items.length && (
        <p className="muted small">
          Showing the newest {data.items.length} of {data.total} tasks.
        </p>
      )}
      <div className="board">
        {boardSections(tasks, collapsed).map((section) =>
          section.type === "chip" ? (
            <div key={section.columns.map((c) => c.title).join("|")} className="column-empty-stack">
              {section.columns.map((column) => (
                <div
                  key={column.title}
                  className="column-chip"
                  aria-label={`${column.title}: ${column.count}`}
                >
                  <span className="column-chip-label">
                    {column.title} <span className="count">{column.count}</span>
                  </span>
                  {column.count > 0 && (
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
      {archiving && <ArchiveTasks onClose={() => setArchiving(false)} />}
      {creating && <NewTask onClose={() => setCreating(false)} />}
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
      <button type="button" className="task-card" onClick={() => onOpen(task.id)}>
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
  ];
}

function NewTask({ onClose }: { onClose: () => void }) {
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
    project: "",
    deliverable: "response" as Deliverable,
    priority: 0,
    capabilities: "",
    accountProviders: "",
    timeoutMinutes: "",
    requires_approval: false,
    review: "none" as "none" | "human" | "orchestrator",
    difficulty: "" as "" | "trivial" | "simple" | "standard" | "hard",
    harnessModel: "",
    executorModel: "",
    harnessAccount: "",
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
  // The kind's default capabilities apply when the field is empty, as on the server.
  const typedCapabilities = form.capabilities
    .split(",")
    .map((c) => c.trim())
    .filter(Boolean);
  const capabilities = typedCapabilities.length
    ? typedCapabilities
    : (kinds?.find((k) => k.name === form.kind)?.default_capabilities ?? []);
  const need = repositoryNeed(form.deliverable, capabilities);

  async function submit(e: FormEvent) {
    e.preventDefault();
    await action.run(async () => {
      try {
        const capabilities = form.capabilities
          .split(",")
          .map((c) => c.trim())
          .filter(Boolean);
        const accountProviders = form.accountProviders
          .split(",")
          .map((c) => c.trim())
          .filter(Boolean);
        const harness = harnessPin(form.harnessModel);
        const executor = executorPin(form.executorModel);
        await call(
          api.POST("/api/tasks", {
            body: {
              title: form.title,
              spec: form.spec,
              kind: form.kind,
              project,
              deliverable: form.deliverable,
              priority: form.priority,
              requires_approval: form.requires_approval,
              review: form.review,
              ...(form.difficulty ? { difficulty: form.difficulty } : {}),
              ...(form.timeoutMinutes ? { timeout_s: Number(form.timeoutMinutes) * 60 } : {}),
              ...(capabilities.length ? { required_capabilities: capabilities } : {}),
              ...(accountProviders.length ? { account_providers: accountProviders } : {}),
              ...(harness ? { harness_model: harness } : {}),
              ...(executor ? { executor_model: executor } : {}),
              ...(form.harnessAccount ? { harness_account: form.harnessAccount } : {}),
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
            <label>
              Deliverable
              <select
                value={form.deliverable}
                onChange={(e) => set("deliverable", e.target.value as Deliverable)}
              >
                {DELIVERABLES.map((d) => (
                  <option key={d.value} value={d.value}>
                    {d.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Priority
              <input
                type="number"
                value={form.priority}
                onChange={(e) => set("priority", Number(e.target.value))}
              />
            </label>
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
          <label>
            Difficulty (trivial and simple work goes to a cheap executor; hard work to the
            strongest)
            <select
              value={form.difficulty}
              onChange={(e) => set("difficulty", e.target.value as typeof form.difficulty)}
            >
              <option value="">decide from the title and spec</option>
              <option value="trivial">trivial</option>
              <option value="simple">simple</option>
              <option value="standard">standard</option>
              <option value="hard">hard</option>
            </select>
          </label>
          <label>
            Required capabilities (comma-separated; empty uses the kind's default)
            <input
              value={form.capabilities}
              onChange={(e) => set("capabilities", e.target.value)}
            />
          </label>
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
          <label>
            Timeout in minutes (empty: the kind's, then the server's default)
            <input
              type="number"
              min={1}
              value={form.timeoutMinutes}
              onChange={(e) => set("timeoutMinutes", e.target.value)}
            />
          </label>
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
