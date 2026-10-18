import { type FormEvent, useState } from "react";
import { type Account, api, call } from "../api";

/** Edits registry fields only. The credential is never loaded, shown, or sent
 * from here; it is replaced through Renew credential. */
export function EditAccount({
  account,
  onClose,
  onDone,
}: {
  account: Account;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const [name, setName] = useState(account.name);
  const [priority, setPriority] = useState(String(account.priority));
  const [maxLeases, setMaxLeases] = useState(account.max_leases?.toString() ?? "");
  const [enabled, setEnabled] = useState(account.state !== "disabled");
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(undefined);
    if (name.trim() === "") {
      setError("Name is required.");
      return;
    }
    const nextPriority = Number(priority);
    if (priority.trim() === "" || !Number.isInteger(nextPriority)) {
      setError("Priority must be a whole number.");
      return;
    }
    const limit = maxLeases.trim() === "" ? null : Number(maxLeases);
    if (limit !== null && (!Number.isInteger(limit) || limit < 1)) {
      setError("The lease limit must be a whole number of at least 1, or empty for unlimited.");
      return;
    }
    const body: Record<string, unknown> = {};
    if (name.trim() !== account.name) body.name = name.trim();
    if (nextPriority !== account.priority) body.priority = nextPriority;
    if (limit !== account.max_leases) body.max_leases = limit;
    if (enabled !== (account.state !== "disabled")) body.enabled = enabled;
    if (Object.keys(body).length === 0) {
      onClose();
      return;
    }
    setBusy(true);
    try {
      await call(
        api.PATCH("/api/accounts/{account_id}", {
          params: { path: { account_id: account.id } },
          body,
        }),
      );
      onDone(`Saved “${name.trim()}”.`);
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  return (
    <div className="overlay">
      <form className="dialog" onSubmit={submit} aria-label={`Edit ${account.name}`} noValidate>
        <h2>Edit {account.name}</h2>
        <p className="muted small">
          Provider {account.provider} cannot change. The credential is not shown or edited here; use
          Renew credential where available.
        </p>
        <label>
          Name
          <input value={name} onChange={(e) => setName(e.target.value)} maxLength={100} required />
        </label>
        <div className="row">
          <label>
            Priority (higher first)
            <input type="number" value={priority} onChange={(e) => setPriority(e.target.value)} />
          </label>
          <label>
            Max concurrent leases (empty: unlimited)
            <input
              type="number"
              min={1}
              value={maxLeases}
              onChange={(e) => setMaxLeases(e.target.value)}
            />
          </label>
        </div>
        <label className="check">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          Enabled (a disabled account is never leased)
        </label>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <div className="actions">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="primary" disabled={busy}>
            Save
          </button>
        </div>
      </form>
    </div>
  );
}

/** Deleting needs the account's name typed back. The server checks it again
 * against the current name, so a stale dialog cannot delete a renamed target. */
export function DeleteAccount({
  account,
  onClose,
  onDone,
}: {
  account: Account;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const [typed, setTyped] = useState("");
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (typed !== account.name) return;
    setError(undefined);
    setBusy(true);
    try {
      await call(
        api.DELETE("/api/accounts/{account_id}", {
          params: { path: { account_id: account.id }, query: { confirm_name: typed } },
        }),
      );
      onDone(`Deleted “${account.name}”.`);
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  return (
    <div className="overlay">
      <form className="dialog" onSubmit={submit} aria-label={`Delete ${account.name}`}>
        <h2>Delete {account.name}?</h2>
        <p className="small">
          This removes the {account.provider} account, its usage history and its stored credential.
          It cannot be undone. To keep it but stop agents using it, Disable it instead.
        </p>
        <label>
          Type <strong>{account.name}</strong> to confirm
          <input
            aria-label="Confirm account name"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            autoComplete="off"
            spellCheck={false}
          />
        </label>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <div className="actions">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="danger" disabled={busy || typed !== account.name}>
            Delete account
          </button>
        </div>
      </form>
    </div>
  );
}
