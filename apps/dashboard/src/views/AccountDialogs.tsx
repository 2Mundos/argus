import { type FormEvent, useState } from "react";
import {
  COPILOT_PROVIDER,
  copilotTokenNotice,
  GITEA_PROVIDER,
  GITHUB_PROVIDER,
  validateCopilotToken,
  withoutSecret,
} from "../accountProviders";
import { type Account, api, call } from "../api";
import { parseTokenLimits, TOKEN_LIMIT_PERIODS } from "../usage";

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
  const [limits, setLimits] = useState({
    "5h": account.token_limits?.["5h"]?.toString() ?? "",
    "24h": account.token_limits?.["24h"]?.toString() ?? "",
    "7d": account.token_limits?.["7d"]?.toString() ?? "",
  });
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
    const parsedLimits = parseTokenLimits(limits);
    if ("error" in parsedLimits) {
      setError(parsedLimits.error);
      return;
    }
    const body: Record<string, unknown> = {};
    const canon = (o: Record<string, number> | null | undefined) =>
      JSON.stringify(o ? Object.entries(o).sort() : null);
    if (canon(parsedLimits.limits) !== canon(account.token_limits))
      body.token_limits = parsedLimits.limits;
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
        <fieldset className="row">
          <legend className="muted small">
            Token limits (total tokens per rolling window; empty: none). Used to estimate quota left
            when the provider reports none.
          </legend>
          {TOKEN_LIMIT_PERIODS.map((period) => (
            <label key={period}>
              {period}
              <input
                type="number"
                min={1}
                value={limits[period]}
                onChange={(e) => setLimits({ ...limits, [period]: e.target.value })}
              />
            </label>
          ))}
        </fieldset>
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

/** Replaces the stored credential with a document the operator supplies. The
 * form starts blank and nothing stored is ever read into it. The server stores
 * it after validating its shape; storing is not proof that it authenticates. */
export function ReplaceCredential({
  account,
  onClose,
  onDone,
}: {
  account: Account;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const isGitea = account.provider === GITEA_PROVIDER;
  const isGithub = account.provider === GITHUB_PROVIDER;
  const isCopilot = account.provider === COPILOT_PROVIDER;
  const [text, setText] = useState("");
  const [username, setUsername] = useState("");
  const [token, setToken] = useState("");
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);

  async function pick(file: File | undefined) {
    if (!file) return;
    setError(undefined);
    try {
      setText(await file.text());
    } catch {
      setError("Could not read that file.");
    }
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(undefined);
    let credential: Record<string, unknown>;
    if (isCopilot) {
      const hint = validateCopilotToken(token);
      if (hint) {
        setError(hint);
        return;
      }
      credential = { token: token.trim() };
    } else if (isGitea) {
      credential = { username: username.trim(), token };
    } else if (isGithub) {
      credential = { token };
    } else {
      try {
        const parsed: unknown = JSON.parse(text);
        if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw 0;
        credential = parsed as Record<string, unknown>;
      } catch {
        setError("The credential must be a JSON object.");
        return;
      }
      if (Object.keys(credential).length === 0) {
        setError("The credential must not be an empty object.");
        return;
      }
    }
    setBusy(true);
    try {
      await call(
        api.PUT("/api/accounts/{account_id}/credential", {
          params: { path: { account_id: account.id } },
          body: { credential },
        }),
      );
      setToken("");
      onDone(`Replaced the credential of “${account.name}”.`);
    } catch (err) {
      setError(withoutSecret((err as Error).message, token));
      setToken("");
      setBusy(false);
    }
  }

  return (
    <div className="overlay">
      <form
        className={`dialog${isCopilot ? " copilot-import" : ""}`}
        onSubmit={submit}
        aria-label={`Replace credential of ${account.name}`}
        noValidate
      >
        <h2>Replace credential of {account.name}</h2>
        <p className="muted small">
          Name, provider, priority, lease limit and enabled state stay as they are; a disabled
          account stays disabled. The old credential is overwritten and is never shown. Supply the
          login document the provider's client writes, not a settings file without tokens: Argus
          stores it but cannot tell here whether it signs in.
        </p>
        {isCopilot && <p className="muted small">{copilotTokenNotice()}</p>}
        {isCopilot ? (
          <label>
            Copilot token
            <input
              type="password"
              aria-label="Copilot token"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              autoComplete="off"
              spellCheck={false}
            />
          </label>
        ) : isGitea || isGithub ? (
          <div className="row">
            {isGitea && (
              <label>
                Gitea username
                <input
                  aria-label="Gitea username"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  autoComplete="off"
                />
              </label>
            )}
            <label>
              {isGitea ? "Gitea" : "GitHub"} token
              <input
                type="password"
                aria-label={isGitea ? "Gitea token" : "GitHub token"}
                value={token}
                onChange={(e) => setToken(e.target.value)}
                autoComplete="off"
              />
            </label>
          </div>
        ) : (
          <>
            <label>
              New credential (JSON object; stored encrypted)
              <textarea
                aria-label="New credential"
                rows={6}
                value={text}
                onChange={(e) => setText(e.target.value)}
                spellCheck={false}
                autoComplete="off"
              />
            </label>
            <label>
              Or choose a JSON file
              <input
                type="file"
                aria-label="Credential file"
                accept=".json,application/json"
                onChange={(e) => pick(e.target.files?.[0])}
              />
            </label>
          </>
        )}
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
            Replace credential
          </button>
        </div>
      </form>
    </div>
  );
}
