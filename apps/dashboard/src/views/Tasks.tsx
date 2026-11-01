import { type FormEvent, useState } from "react";
import { ActionButton, useAction } from "../ActionButton";
import { api, call, newestTasks, type TaskKind, type TaskState, type TaskSummary } from "../api";
import { DateChip } from "../DateChip";
import { DELIVERABLES, type Deliverable } from "../deliverables";
import { reportPage, useLiveData } from "../live";
import { TaskSignal } from "../milestones";
import { PageHeader } from "../PageHeader";
import { TaskLabel } from "../TaskLabel";
import { executorPin, harnessPin } from "../taskEdit";
import { WorkerChip } from "../WorkerChip";
import { AccountPin } from "./AccountPin";
import { useNow } from "./Activity";
import { ArchiveTasks } from "./ArchiveTasks";
import { ModelPin } from "./ModelPin";
import { ConfiguredIcon, ProviderMark, providerForTool } from "./providerIcons";
import { TaskDetail } from "./TaskDetail";

type UsedModel = NonNullable<NonNullable<TaskSummary["models"]>["used"]>[number];

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
  );
  const tasks = data?.items;

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
                  <TaskCard key={task.id} task={task} onOpen={() => setSelected(task.id)} />
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

/** Compact chip: engine mark + model id; gear means configured, not yet observed. */
function ModelChip({ item }: { item: UsedModel }) {
  const tool = item.tool ?? item.layer;
  const model = item.model ?? "?";
  const configured = !item.observed && !!item.model;
  const title = `${tool} · ${model}${configured ? " · configured" : ""}`;
  return (
    <span className="badge model-chip" title={title}>
      <ProviderMark provider={providerForTool(item.tool ?? item.layer)} />
      <span className="model-chip-id">{model}</span>
      {configured ? <ConfiguredIcon /> : null}
    </span>
  );
}

function TaskCard({ task, onOpen }: { task: TaskSummary; onOpen: () => void }) {
  const now = useNow(30_000);
  return (
    <button type="button" className="task-card" onClick={onOpen}>
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
            <ModelChip key={`${item.layer}-${item.tool}-${item.model}`} item={item} />
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
  const action = useAction();
  const set = <K extends keyof typeof form>(key: K, value: (typeof form)[K]) =>
    setForm((f) => ({ ...f, [key]: value }));
  // `global` scopes knowledge and settings only; a task needs a real project.
  const taskProjects = (projects ?? []).filter((p) => p.slug !== "global");
  const project = form.project || taskProjects[0]?.slug || "";

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
        <label>
          Difficulty (trivial and simple work goes to a cheap executor; hard work to the strongest)
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
          <input value={form.capabilities} onChange={(e) => set("capabilities", e.target.value)} />
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
  );
}
