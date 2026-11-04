import { ApiError, api, call, type Project, type ProjectStats } from "./api";
import { CODE_DELIVERABLES, type Deliverable } from "./deliverables";

/**
 * Why `raw` is not a repository Argus accepts, or null when it is. Mirrors the API's rule
 * (HTTPS `host/owner/repository`, no credentials, query or fragment); the API stays the
 * authority and its refusal is shown as well.
 */
export function repositoryProblem(raw: string): string | null {
  const text = raw.trim();
  if (!text) return "Enter the repository URL.";
  let url: URL;
  try {
    url = new URL(text);
  } catch {
    return "That is not a URL. Use https://host/owner/repository.";
  }
  if (url.protocol !== "https:") return "The repository must be an HTTPS URL.";
  if (url.username || url.password) {
    return "Remove the credentials from the URL; Argus injects access separately.";
  }
  if (url.search || url.hash) return "Remove the query or fragment from the URL.";
  if (url.pathname.split("/").filter(Boolean).length !== 2) {
    return "Use the form https://host/owner/repository.";
  }
  return null;
}

/** What a failed save or load means for the person who triggered it. */
export function repositoryFailure(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.code === "repository_admin_only" || err.status === 403) {
      return "Only an admin can change a project's repository. Ask an admin, or sign in with an admin token.";
    }
    if (err.status === 401) return "Your session is no longer valid. Sign in again.";
    if (err.status === 404) return `The project no longer exists: ${err.message}`;
    return err.details.length ? `${err.message}: ${err.details.join("; ")}` : err.message;
  }
  return "Could not reach Argus. Check the connection and try again.";
}

export async function loadProject(slug: string, signal?: AbortSignal): Promise<Project | null> {
  const projects = await call(api.GET("/api/projects", { signal }));
  return projects.find((p) => p.slug === slug) ?? null;
}

/**
 * Set (or with null clear) a project's repository through the admin-only route that
 * changes nothing else: name and description stay whatever the server holds, so a
 * concurrent edit of them cannot be overwritten.
 */
export function saveRepository(slug: string, url: string | null): Promise<Project> {
  return call(
    api.PATCH("/api/projects/{slug}/repository", {
      params: { path: { slug } },
      body: { repository_url: url },
    }),
  );
}

export type RepositorySource = NonNullable<ProjectStats["repository_source"]>;

/** What the stats say about a project's repository; "unknown" when they could not be read. */
export function repositoryState(
  stats: ProjectStats | undefined,
): "set" | RepositorySource | "absent" | "unknown" {
  if (!stats) return "unknown";
  if (stats.repository_url) return "set";
  return stats.repository_source ?? "absent";
}

export type RepositoryNeed = "refused" | "blocks" | "none";

/**
 * How a task's deliverable and capabilities depend on the repository. Argus refuses to
 * create a branch/PR task whose project has none; a task holding `repository:write`
 * (set, or the kind's default) is blocked by a worker that cannot resolve one.
 */
export function repositoryNeed(deliverable: Deliverable, capabilities: string[]): RepositoryNeed {
  if (CODE_DELIVERABLES.includes(deliverable)) return "refused";
  return capabilities.includes("repository:write") ? "blocks" : "none";
}

/** The warning for a project with no usable repository, worded for what is actually true. */
export function repositoryWarning(
  project: string,
  state: "absent" | "invalid" | "conflict",
  need: RepositoryNeed,
): string {
  const head = {
    absent: `Project ${project} has no repository.`,
    invalid: `Project ${project} has a stored repository that cannot be used.`,
    conflict: `The pools name different repositories for ${project}, so Argus cannot pick one.`,
  }[state];
  if (need === "refused") return `${head} Argus will refuse to create this task until one is set.`;
  if (state === "conflict") {
    return need === "blocks"
      ? `${head} Workers whose pool names one may use it and others will block; set one on the project so they agree.`
      : `${head} The task can still run; set one on the project so every worker agrees.`;
  }
  return need === "blocks"
    ? `${head} A worker will block this task until one is set.`
    : `${head} This task can still run, but workers get no checkout of its code.`;
}
