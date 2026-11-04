import { type FormEvent, type KeyboardEvent, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ActionButton, useAction } from "../ActionButton";
import type { Project } from "../api";
import {
  loadProject,
  repositoryFailure,
  repositoryProblem,
  saveRepository,
} from "../projectRepository";

/**
 * Set or clear one project's repository. It is a sibling overlay, never a form inside
 * another one, so a caller keeps its own state: closing it (Cancel, Escape, a save) leaves
 * everything behind it as it was.
 */
export function ProjectRepositoryDialog({
  slug,
  onSaved,
  onCancel,
}: {
  slug: string;
  onSaved: (project: Project) => void;
  onCancel: () => void;
}) {
  const [project, setProject] = useState<Project | null>();
  const [loadError, setLoadError] = useState<string>();
  const [attempt, setAttempt] = useState(0);
  const [value, setValue] = useState("");
  const [touched, setTouched] = useState(false);
  const [error, setError] = useState<string>();
  const action = useAction();

  useEffect(() => {
    const controller = new AbortController();
    setProject(undefined);
    setLoadError(undefined);
    loadProject(slug, controller.signal).then(
      (found) => {
        setProject(found);
        setValue((v) => v || found?.repository_url || "");
      },
      (err) => {
        if (!controller.signal.aborted) setLoadError(repositoryFailure(err));
      },
    );
    return () => controller.abort();
  }, [slug, attempt]);

  const formRef = useRef<HTMLFormElement>(null);
  // Focus lands inside the dialog so Escape reaches it, not the page behind.
  useEffect(() => formRef.current?.focus(), []);

  /** Escape closes only this dialog: a drawer or page below must stay open. */
  function onKeyDown(e: KeyboardEvent<HTMLFormElement>) {
    if (e.key !== "Escape") return;
    e.stopPropagation();
    if (!action.busy) onCancel();
  }

  const problem = touched || value ? repositoryProblem(value) : null;

  async function save(url: string | null) {
    setError(undefined);
    await action.run(
      async () => {
        try {
          onSaved(await saveRepository(slug, url));
        } catch (err) {
          setError(repositoryFailure(err));
        }
      },
      url === null ? "clear" : "save",
    );
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    setTouched(true);
    if (repositoryProblem(value)) return;
    void save(value.trim());
  }

  const ready = project !== undefined && project !== null;
  return createPortal(
    <div className="overlay">
      <form
        ref={formRef}
        tabIndex={-1}
        onKeyDown={onKeyDown}
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-label="Project repository"
        noValidate
        onSubmit={submit}
      >
        <h2>Repository for {project?.name ?? slug}</h2>
        <p className="muted small">
          The HTTPS URL workers clone for this project. It is a pointer, not access: a task still
          needs a repository account. Do not put credentials in the URL. Only an admin can change
          it.
        </p>
        {project === undefined && !loadError && <p className="muted">Loading the project…</p>}
        {loadError && (
          <p className="error" role="alert">
            {loadError}{" "}
            <button type="button" className="link" onClick={() => setAttempt((n) => n + 1)}>
              Retry
            </button>
          </p>
        )}
        {project === null && (
          <p className="error" role="alert">
            The project {slug} no longer exists.
          </p>
        )}
        {ready && (
          <label>
            Repository URL
            <input
              type="url"
              inputMode="url"
              autoComplete="off"
              spellCheck={false}
              placeholder="https://github.com/owner/repository"
              value={value}
              onChange={(e) => setValue(e.target.value)}
              onBlur={() => setTouched(true)}
              aria-invalid={problem ? true : undefined}
              aria-describedby={problem ? "repository-problem" : undefined}
              autoFocus
            />
          </label>
        )}
        {problem && (
          <p id="repository-problem" className="error" role="alert">
            {problem}
          </p>
        )}
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <div className="actions">
          <button type="button" disabled={action.busy} onClick={onCancel}>
            Cancel
          </button>
          {ready && project.repository_url && (
            <ActionButton
              type="button"
              pending={action.is("clear")}
              disabled={action.busy}
              action={() => save(null)}
            >
              Clear repository
            </ActionButton>
          )}
          <ActionButton
            type="submit"
            className="primary"
            pending={action.is("save")}
            disabled={!ready || action.busy}
          >
            Save repository
          </ActionButton>
        </div>
      </form>
    </div>,
    document.body,
  );
}
