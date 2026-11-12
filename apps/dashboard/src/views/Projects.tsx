import { useState } from "react";
import { api, call, type Project } from "../api";
import { DateChip } from "../DateChip";
import { useLiveData } from "../live";
import { PageHeader } from "../PageHeader";
import { fetchSettings } from "../settings";
import { useHash } from "../unsaved";
import { GitIdentityEditor } from "./GitIdentityEditor";
import { ProjectGraph } from "./ProjectGraph";
import { ProjectRepositoryDialog } from "./ProjectRepositoryDialog";
import { Settings, settingsHash } from "./Settings";
import { TaskDefaultsEditor } from "./TaskDefaultsEditor";
import { Tasks } from "./Tasks";

type Stats = Awaited<ReturnType<typeof loadStats>>[number];

function loadStats(signal: AbortSignal) {
  return call(api.GET("/api/projects/stats", { signal }));
}

const STATE_ORDER = [
  "ready",
  "claimed",
  "blocked",
  "awaiting_approval",
  "in_review",
  "pending",
  "succeeded",
  "failed",
  "cancelled",
];

/** Task counts in a stable order; states the API reports beyond the known ones follow. */
export function taskCounts(tasks: Record<string, number>): [string, number][] {
  const known = STATE_ORDER.filter((s) => (tasks[s] ?? 0) > 0).map((s): [string, number] => [
    s,
    tasks[s] ?? 0,
  ]);
  const extra = Object.entries(tasks)
    .filter(([s, n]) => n > 0 && !STATE_ORDER.includes(s))
    .sort(([a], [b]) => a.localeCompare(b));
  return [...known, ...extra];
}

const SECTIONS = {
  overview: "Overview",
  tasks: "Tasks",
  graph: "Graph",
  settings: "Settings",
  "task-defaults": "Task defaults",
} as const;
type Section = keyof typeof SECTIONS;

/** `#projects` lists every project; `#projects/<slug>[/<section>]` opens one. */
export function projectRoute(hash: string): { slug: string | null; section: Section } {
  const [, slug, section] = hash.replace(/^#/, "").split("/");
  return {
    slug: slug ? decodeURIComponent(slug) : null,
    section: section && section in SECTIONS ? (section as Section) : "overview",
  };
}

export const projectHash = (slug: string, section: Section = "overview") =>
  `#projects/${encodeURIComponent(slug)}${section === "overview" ? "" : `/${section}`}`;

export function Projects() {
  const route = projectRoute(useHash());
  const {
    data: projects,
    error: projectsError,
    reload: reloadProjects,
  } = useLiveData((signal) => call(api.GET("/api/projects", { signal })), []);
  const { data: stats, error: statsError, reload: reloadStats } = useLiveData(loadStats, []);
  const [editing, setEditing] = useState<string | null>(null);
  const [notice, setNotice] = useState<string>();

  const byslug = new Map((stats ?? []).map((s) => [s.slug, s]));

  function saved(p: Project) {
    setEditing(null);
    setNotice(
      p.repository_url ? `Repository for ${p.name} saved.` : `Repository for ${p.name} cleared.`,
    );
    reloadProjects();
    reloadStats();
  }

  const cardFor = (p: Project) => (
    <ProjectCard
      key={p.slug}
      project={p}
      stats={byslug.get(p.slug)}
      statsUnknown={stats === undefined}
      linked={route.slug === null}
      onEdit={() => {
        setNotice(undefined);
        setEditing(p.slug);
      }}
    />
  );
  const selected = route.slug ? projects?.find((p) => p.slug === route.slug) : undefined;
  const workspaceProjects = (projects ?? []).filter((p) => p.slug !== "global");

  return (
    <section className="projects">
      {route.slug === null ? (
        <PageHeader
          title="Projects"
          lead="Each project's repository, task defaults and what Argus holds for it. Workers clone the repository when they claim a task."
        />
      ) : (
        <PageHeader title={selected?.name ?? route.slug} lead={selected?.description || undefined}>
          <label>
            Project{" "}
            <select
              aria-label="Project"
              value={route.slug}
              onChange={(e) => {
                window.location.hash = projectHash(e.target.value, route.section);
              }}
            >
              {!selected && <option value={route.slug}>{route.slug}</option>}
              {workspaceProjects.map((p) => (
                <option key={p.slug} value={p.slug}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
          <a href="#projects">All projects</a>
        </PageHeader>
      )}
      <p className="muted small">
        Changing a repository or a task default needs an admin token. Other tokens can read this
        page but are refused when they save.
      </p>
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      {projectsError && (
        <p className="error" role="alert">
          Could not load projects: {projectsError.message}{" "}
          <button type="button" className="link" onClick={reloadProjects}>
            Retry
          </button>
        </p>
      )}
      {statsError && (
        <p className="error" role="alert">
          Project statistics are unavailable: {statsError.message}. Repositories shown may be
          incomplete.{" "}
          <button type="button" className="link" onClick={reloadStats}>
            Retry
          </button>
        </p>
      )}
      {projects === undefined && !projectsError && <p className="muted">Loading projects…</p>}
      {route.slug === null ? (
        <>
          {projects?.length === 0 && <p className="muted">No projects yet.</p>}
          <div className="project-list">{(projects ?? []).map(cardFor)}</div>
        </>
      ) : (
        projects !== undefined &&
        (route.slug === "global" ? (
          <p className="muted">
            The global scope has no repository or tasks of its own; its task defaults are the{" "}
            <a href={settingsHash("global")}>global settings</a>.
          </p>
        ) : !selected ? (
          <p className="error" role="alert">
            There is no project <code>{route.slug}</code>. <a href="#projects">All projects</a>
          </p>
        ) : (
          <>
            <nav className="subtabs" aria-label="Project sections">
              {(Object.keys(SECTIONS) as Section[]).map((section) => (
                <a
                  key={section}
                  href={projectHash(selected.slug, section)}
                  aria-current={section === route.section ? "page" : undefined}
                >
                  {SECTIONS[section]}
                </a>
              ))}
              <a href={settingsHash(selected.slug)}>Other settings</a>
            </nav>
            {route.section === "tasks" ? (
              <Tasks project={selected.slug} showHeader={false} />
            ) : route.section === "settings" ? (
              <Settings scope={selected.slug} showHeader={false} />
            ) : route.section === "task-defaults" ? (
              <div className="card">
                <TaskDefaultsEditor key={selected.slug} project={selected.slug} />
              </div>
            ) : route.section === "graph" ? (
              <div className="card">
                <ProjectGraph key={selected.slug} project={selected.slug} />
              </div>
            ) : (
              cardFor(selected)
            )}
          </>
        ))
      )}
      {editing && (
        <ProjectRepositoryDialog slug={editing} onCancel={() => setEditing(null)} onSaved={saved} />
      )}
    </section>
  );
}

function ProjectCard({
  project,
  stats,
  statsUnknown,
  linked,
  onEdit,
}: {
  project: Project;
  stats: Stats | undefined;
  statsUnknown: boolean;
  /** In the list, the name opens the project's workspace. */
  linked: boolean;
  onEdit: () => void;
}) {
  const isGlobal = project.slug === "global";
  return (
    <article className={`card project-card ${linked ? "project-card-concise" : ""}`}>
      <header>
        <div className="project-identity">
          <h3>
            {linked && !isGlobal ? (
              <a href={projectHash(project.slug)}>{project.name}</a>
            ) : (
              project.name
            )}
          </h3>
          <code className="muted small">{project.slug}</code>
          {linked && project.description && (
            <span className="muted small project-desc-inline">{project.description}</span>
          )}
        </div>
        {!isGlobal && linked && (
          <div className="actions right">
            <button type="button" className="compact" onClick={onEdit}>
              {project.repository_url ? "Edit repository" : "Set repository"}
            </button>
            <a href={projectHash(project.slug)} className="button compact secondary">
              Details →
            </a>
          </div>
        )}
      </header>
      {!linked && project.description && <p className="muted">{project.description}</p>}
      <dl className="facts">
        <dt>Repository</dt>
        <dd>
          <Repository project={project} stats={stats} statsUnknown={statsUnknown} />
        </dd>
        {stats && (
          <>
            <dt>Tasks</dt>
            <dd>
              {taskCounts(stats.tasks).length === 0 ? (
                <span className="muted">none</span>
              ) : (
                taskCounts(stats.tasks).map(([state, n]) => (
                  <span key={state} className={`badge state-${state}`}>
                    {n} {state.replace("_", " ")}
                  </span>
                ))
              )}
              {stats.archived_tasks > 0 && (
                <span className="muted small"> · {stats.archived_tasks} archived</span>
              )}
            </dd>
            <dt>Last task activity</dt>
            <dd>
              {stats.last_task_activity_at ? (
                <DateChip at={stats.last_task_activity_at} />
              ) : (
                <span className="muted">no tasks yet</span>
              )}
            </dd>
            <dt>Knowledge pages</dt>
            <dd>{stats.knowledge_pages}</dd>
          </>
        )}
      </dl>
      {!isGlobal && (
        <div className="project-git-author-section">
          <ProjectGitIdentity slug={project.slug} />
        </div>
      )}
      {!isGlobal && !linked && (
        <div className="actions left">
          <button type="button" onClick={onEdit}>
            {project.repository_url ? "Edit repository" : "Set repository"}
          </button>
        </div>
      )}
    </article>
  );
}

function Repository({
  project,
  stats,
  statsUnknown,
}: {
  project: Project;
  stats: Stats | undefined;
  statsUnknown: boolean;
}) {
  if (project.slug === "global") {
    return <span className="muted">Not applicable: global holds knowledge and settings only.</span>;
  }
  if (stats?.repository_source === "invalid") {
    return (
      <>
        <code>{project.repository_url}</code>
        <span className="muted small"> is stored but cannot be used; set a valid repository.</span>
      </>
    );
  }
  if (stats?.repository_source === "conflict") {
    return (
      <span className="muted">
        The pools name different repositories for this project, so Argus cannot pick one. Set it
        here to make every worker agree.
      </span>
    );
  }
  if (stats?.repository_source === "pools" && stats.repository_url) {
    return (
      <>
        <code>{stats.repository_url}</code>
        <span className="muted small">
          {" "}
          from the pools' allowlists
          {project.repository_url ? "; the stored value is not usable" : "; not set on the project"}
        </span>
      </>
    );
  }
  if (project.repository_url) {
    return <code>{project.repository_url}</code>;
  }
  if (statsUnknown) {
    return <span className="muted">Not set on the project; pool allowlists not checked.</span>;
  }
  return <span className="muted">Not configured. Tasks that push code will block.</span>;
}

/** Whether the project inherits the global git identity or overrides it (settings.spec.md 3.8). */
function ProjectGitIdentity({ slug }: { slug: string }) {
  const { data, error, reload } = useLiveData((signal) => fetchSettings(slug, signal), [slug]);
  const setting = (data?.settings ?? []).find((s) => s.key === "git.identity");
  return (
    <div>
      <h4>Git author</h4>
      {error && (
        <p className="error" role="alert">
          Could not load the git author: {error.message}
        </p>
      )}
      {data && <GitIdentityEditor project={slug} setting={setting} onChanged={reload} />}
    </div>
  );
}
