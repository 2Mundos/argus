import { useState } from "react";
import { ActionButton, useAction } from "../ActionButton";
import { ApiError, api, call } from "../api";
import { useLiveData } from "../live";
import { repositoryFailure, repositoryState } from "../projectRepository";
import { ProjectRepositoryDialog } from "./ProjectRepositoryDialog";

/**
 * What to do about a task blocked with `env_repository_unconfigured`. The block was
 * recorded when a worker looked, so the repository may since have been set elsewhere (by
 * an admin, MCP, or a pool change): the project's current state decides what is offered.
 * Setting it needs an admin; unblocking is explicit and offered whenever a repository now
 * resolves, never done for the person. After it succeeds the controls go away.
 */
export function RepositoryBlockAction({
  taskId,
  project,
  onUnblocked,
}: {
  taskId: string;
  project: string;
  onUnblocked?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [saved, setSaved] = useState<string | null>(); // null: cleared
  const [done, setDone] = useState<"unblocked" | "already">();
  const [error, setError] = useState<string>();
  const action = useAction();
  const { data: stats, error: statsError } = useLiveData(
    (signal) => call(api.GET("/api/projects/stats", { signal })),
    [project, saved],
  );
  const state =
    saved !== undefined
      ? saved
        ? "set"
        : "absent"
      : repositoryState(stats?.find((s) => s.slug === project));

  async function unblock() {
    setError(undefined);
    await action.run(async () => {
      try {
        await call(
          api.POST("/api/tasks/{task_id}/unblock", { params: { path: { task_id: taskId } } }),
        );
        setDone("unblocked");
        onUnblocked?.();
      } catch (err) {
        if (err instanceof ApiError && err.status === 409) {
          // Someone else already moved it on: nothing left to do, and not a failure.
          setDone("already");
          onUnblocked?.();
        } else {
          setError(repositoryFailure(err));
        }
      }
    });
  }

  if (done) {
    return (
      <p className="small" role="status">
        {done === "unblocked"
          ? "Unblocked: a worker can claim the task again."
          : "This task was already unblocked."}
      </p>
    );
  }

  const resolved = state === "set" || state === "pools";
  return (
    <div className="repo-block-action">
      <p className="small">
        {saved
          ? `Repository saved: ${saved}. Unblock the task so a worker can claim it.`
          : resolved
            ? `Project ${project} now has a repository. Unblock the task so a worker retries it.`
            : state === "conflict"
              ? `The pools name different repositories for ${project}, so no worker could pick one. Set one on the project, then unblock.`
              : state === "invalid"
                ? `Project ${project} has a stored repository that cannot be used. Set a valid one, then unblock.`
                : state === "unknown"
                  ? `Could not check ${project}'s repository${statsError ? ` (${statsError.message})` : ""}. Only an admin can set it.`
                  : `Project ${project} has no repository, and this task needs one. Only an admin can set it.`}
      </p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="actions left">
        <button type="button" disabled={action.busy} onClick={() => setOpen(true)}>
          {saved || resolved ? "Change repository" : "Set repository…"}
        </button>
        {(saved || resolved) && (
          <ActionButton className="primary" pending={action.busy} action={unblock}>
            Unblock task
          </ActionButton>
        )}
      </div>
      {open && (
        <ProjectRepositoryDialog
          slug={project}
          onCancel={() => setOpen(false)}
          onSaved={(p) => {
            setSaved(p.repository_url);
            setOpen(false);
          }}
        />
      )}
    </div>
  );
}
