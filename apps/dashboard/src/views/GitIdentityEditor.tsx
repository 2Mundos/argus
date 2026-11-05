import { useEffect, useState } from "react";
import { ActionButton, useAction } from "../ActionButton";
import { ApiError } from "../api";
import { identityOf, originOf, validateIdentity } from "../gitIdentity";
import { putSetting, resetSetting, type SettingValue } from "../settings";

const KEY = "git.identity";

function describe(e: unknown): string {
  if (e instanceof ApiError && e.status === 403) {
    return "Only an admin token can change the git author identity.";
  }
  const details = e instanceof ApiError && e.details.length ? `: ${e.details.join("; ")}` : "";
  return `${e instanceof Error ? e.message : String(e)}${details}`;
}

/**
 * Git author and committer for worker commits (settings.spec.md 3.8). With `project` null it
 * edits the global value; otherwise the project override, and Reset restores inheritance
 * without touching the global value.
 */
export function GitIdentityEditor(props: {
  project: string | null;
  setting?: SettingValue;
  onChanged: () => void;
}) {
  const { project, setting, onChanged } = props;
  const stored = identityOf(setting);
  const origin = originOf(project, setting);
  const [name, setName] = useState(stored?.name ?? "");
  const [email, setEmail] = useState(stored?.email ?? "");
  const action = useAction();
  const [failure, setFailure] = useState<string>();
  const [done, setDone] = useState<string>();

  useEffect(() => {
    setName(stored?.name ?? "");
    setEmail(stored?.email ?? "");
  }, [stored?.name, stored?.email]);

  async function save() {
    const checked = validateIdentity(name, email);
    setDone(undefined);
    if (!checked.ok) {
      setFailure(checked.error);
      return;
    }
    await action.run(async () => {
      setFailure(undefined);
      try {
        await putSetting(KEY, checked.identity, project);
        setDone("Saved.");
        onChanged();
      } catch (e) {
        setFailure(describe(e));
      }
    }, "save");
  }

  async function reset() {
    await action.run(async () => {
      setFailure(undefined);
      setDone(undefined);
      try {
        await resetSetting(KEY, project);
        setDone("Reset to inherited.");
        onChanged();
      } catch (e) {
        setFailure(describe(e));
      }
    }, "reset");
  }

  const scopeLabel = project === null ? "global" : `project ${project}`;
  return (
    <div className="git-identity">
      <p className="small">
        {origin === "override" && <span className="badge">override</span>}
        {origin === "inherited" && project !== null && (
          <span className="badge">inherits global</span>
        )}
        {origin === "unset" && <span className="badge">not set</span>}{" "}
        {stored ? (
          <span>
            {stored.name} &lt;{stored.email}&gt;
          </span>
        ) : (
          <span className="muted">Workers cannot commit until an identity is set.</span>
        )}
      </p>
      <div className="row">
        <label>
          Name{" "}
          <input
            aria-label={`git author name (${scopeLabel})`}
            value={name}
            maxLength={200}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <label>
          Email{" "}
          <input
            aria-label={`git author email (${scopeLabel})`}
            value={email}
            maxLength={320}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>
        <ActionButton
          pending={action.is("save")}
          disabled={action.busy}
          action={save}
          pendingLabel="Saving…"
        >
          {project === null ? "Save" : "Save override"}
        </ActionButton>
        {project !== null && (
          <ActionButton
            pending={action.is("reset")}
            disabled={action.busy || origin !== "override"}
            action={reset}
            pendingLabel="Resetting…"
          >
            Reset to inherited
          </ActionButton>
        )}
      </div>
      {failure && (
        <p className="error" role="alert">
          {failure}
        </p>
      )}
      {done && (
        <p className="small" role="status">
          {done}
        </p>
      )}
    </div>
  );
}
