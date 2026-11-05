import { useState } from "react";
import { api, call, type Project } from "../api";
import { DateChip } from "../DateChip";
import { useLiveData } from "../live";
import { PageHeader } from "../PageHeader";
import { fetchSettings } from "../settings";
import { GitIdentityEditor } from "./GitIdentityEditor";
import { ProjectRepositoryDialog } from "./ProjectRepositoryDialog";

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

export function Projects() {
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

  return (
    <section className="projects">
      <PageHeader
        title="Projects"
        lead="Each project's repository and what Argus holds for it. Workers clone the repository when they claim a task."
      />
      <p className="muted small">
        Changing a repository needs an admin token. Other tokens can read this page but are refused
        when they save.
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
      {projects?.length === 0 && <p className="muted">No projects yet.</p>}
      <div className="project-list">
        {(projects ?? []).map((p) => (
          <ProjectCard
            key={p.slug}
            project={p}
            stats={byslug.get(p.slug)}
            statsUnknown={stats === undefined}
            onEdit={() => {
              setNotice(undefined);
              setEditing(p.slug);
            }}
          />
        ))}
      </div>
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
  onEdit,
}: {
  project: Project;
  stats: Stats | undefined;
  statsUnknown: boolean;
  onEdit: () => void;
}) {
  const isGlobal = project.slug === "global";
  return (
    <article className="card project-card">
      <header>
        <h3>{project.name}</h3>
        <code className="muted small">{project.slug}</code>
      </header>
      {project.description && <p className="muted">{project.description}</p>}
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
      {!isGlobal && <ProjectGitIdentity slug={project.slug} />}
      {!isGlobal && (
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
