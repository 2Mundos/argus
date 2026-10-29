import { type FormEvent, useState } from "react";
import { api, call, newestTasks, type TaskKind, type TaskState, type TaskSummary } from "../api";
import { relative, shortId } from "../format";
import { reportPage, useLiveData } from "../live";
import { TaskSignal } from "../milestones";
import { TaskLabel } from "../TaskLabel";
import { executorPin, harnessPin } from "../taskEdit";
import { AccountPin } from "./AccountPin";
import { useNow } from "./Activity";
import { ArchiveTasks } from "./ArchiveTasks";
import { ModelPin } from "./ModelPin";
import { TaskDetail } from "./TaskDetail";

const COLUMNS: { title: string; states: TaskState[] }[] = [
  { title: "Waiting", states: ["awaiting_approval", "pending", "blocked"] },
  { title: "Ready", states: ["ready"] },
  { title: "Running", states: ["claimed"] },
  { title: "In review", states: ["in_review"] },
  { title: "Done", states: ["succeeded"] },
  { title: "Failed / cancelled", states: ["failed", "cancelled"] },
];

export function Tasks() {
  // `#tasks/<id>` (linked from Fleet) opens that task's detail.
  const [selected, setSelected] = useState<string | null>(
    () => window.location.hash.match(/^#tasks\/(.+)$/)?.[1] ?? null,
  );
  const [creating, setCreating] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const [archived, setArchived] = useState(false);
  const { data, error } = useLiveData(
    (signal) => newestTasks(5, archived, { signal, onPage: (page) => reportPage(signal, page) }),
    [archived],
  );
  const tasks = data?.items;

  return (
    <section className={selected ? "tasks-open" : "tasks-page"}>
      <div className="toolbar">
        <h2>Tasks</h2>
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
      </div>
      <p className="page-lead">Work waiting, running and finished.</p>
      {archived && <p className="muted small">Showing archived tasks only.</p>}
      {error && <p className="error">{error.message}</p>}
      {data && data.total > data.items.length && (
        <p className="muted small">
          Showing the newest {data.items.length} of {data.total} tasks.
        </p>
      )}
      <div className="board">
        {COLUMNS.map((column) => {
          const items = (tasks ?? []).filter((t) => column.states.includes(t.state));
          return (
            <div className="column" key={column.title}>
              <h3>
                {column.title} <span className="count">{items.length}</span>
              </h3>
              <div className="column-body">
                {items.length === 0 && <p className="muted small">None</p>}
                {items.map((task) => (
                  <TaskCard key={task.id} task={task} onOpen={() => setSelected(task.id)} />
                ))}
              </div>
            </div>
          );
        })}
      </div>
      {archiving && <ArchiveTasks onClose={() => setArchiving(false)} />}
      {creating && <NewTask onClose={() => setCreating(false)} />}
      {selected && (
        <TaskDetail
          taskId={selected}
          onClose={() => {
            setSelected(null);
            if (window.location.hash.startsWith("#tasks/")) window.location.hash = "tasks";
          }}
        />
      )}
    </section>
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
            <span className="badge" key={`${item.layer}-${item.tool}-${item.model}`}>
              {item.tool ?? item.layer} · {item.model ?? "?"}
              {!item.observed && item.model ? " · configured" : ""}
            </span>
          ))}
        </span>
      ) : null}
      {task.state === "claimed" && (
        <span className="muted small">
          {shortId(task.assignee_agent_id ?? "")} · lease {relative(task.lease_expires_at)}
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
    project: "global",
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
  const set = <K extends keyof typeof form>(key: K, value: (typeof form)[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  async function submit(e: FormEvent) {
    e.preventDefault();
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
            project: form.project,
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
            <select value={form.project} onChange={(e) => set("project", e.target.value)}>
              {(projects ?? []).map((p) => (
                <option key={p.slug} value={p.slug}>
                  {p.name}
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
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="primary">
            Create
          </button>
        </div>
      </form>
    </div>
  );
}
