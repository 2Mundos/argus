import { type FormEvent, useEffect, useState } from "react";
import { ActionButton, useAction } from "../ActionButton";
import {
  COPILOT_PROVIDER,
  CUSTOM_PROVIDER_PATTERN,
  copilotTokenNotice,
  GITEA_PROVIDER,
  GITHUB_PROVIDER,
  localBrokerNotice,
  nativeAuthNotice,
  PROVIDER_PICK_CUSTOM,
  providerOptionsFromCatalog,
  validateCopilotToken,
  validateProviderId,
  withoutSecret,
} from "../accountProviders";
import { type Account, api, call } from "../api";
import { useLiveData } from "../live";
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
  const action = useAction();

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
    await action.run(async () => {
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
      }
    });
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
          <button type="button" disabled={action.busy} onClick={onClose}>
            Cancel
          </button>
          <ActionButton type="submit" className="primary" pending={action.busy}>
            Save
          </ActionButton>
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
  const action = useAction();

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (typed !== account.name) return;
    setError(undefined);
    await action.run(async () => {
      try {
        await call(
          api.DELETE("/api/accounts/{account_id}", {
            params: { path: { account_id: account.id }, query: { confirm_name: typed } },
          }),
        );
        onDone(`Deleted “${account.name}”.`);
      } catch (err) {
        setError((err as Error).message);
      }
    });
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
          <button type="button" disabled={action.busy} onClick={onClose}>
            Cancel
          </button>
          <ActionButton
            type="submit"
            className="danger"
            disabled={typed !== account.name}
            pending={action.busy}
          >
            Delete account
          </ActionButton>
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
  const action = useAction();

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
    await action.run(async () => {
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
      }
    });
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
          <button type="button" disabled={action.busy} onClick={onClose}>
            Cancel
          </button>
          <ActionButton type="submit" className="primary" pending={action.busy}>
            Replace credential
          </ActionButton>
        </div>
      </form>
    </div>
  );
}

/** Add-account form for any provider. `initialProvider` preselects one once the catalogue loads. */
export function AddAccountForm({
  initialProvider,
  onClose,
  onDone = onClose,
}: {
  initialProvider?: string;
  onClose: () => void;
  onDone?: () => void;
}) {
  const {
    data: catalog,
    error: catalogError,
    reload: reloadCatalog,
  } = useLiveData((signal) => call(api.GET("/api/engines", { signal })), []);
  const providerOptions = catalog ? providerOptionsFromCatalog(catalog) : [];
  const [providerPick, setProviderPick] = useState("");
  const [customProvider, setCustomProvider] = useState("");
  const [form, setForm] = useState({
    name: "",
    kind: "api_key" as "api_key" | "subscription",
    priority: 0,
    maxLeases: "",
    credential: '{\n  "api_key": ""\n}',
    repoUsername: "",
    repoToken: "",
  });
  const [error, setError] = useState<string>();
  const action = useAction();
  const set = <K extends keyof typeof form>(key: K, value: (typeof form)[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  useEffect(() => {
    if (providerPick || providerOptions.length === 0) return;
    const wanted = providerOptions.find((o) => o.id === initialProvider) ?? providerOptions[0];
    if (wanted) setProviderPick(wanted.id);
  }, [providerOptions, providerPick, initialProvider]);

  const resolvedProvider =
    providerPick === PROVIDER_PICK_CUSTOM ? customProvider.trim() : providerPick;
  const authNotice = nativeAuthNotice(resolvedProvider);
  const isGitea = resolvedProvider === GITEA_PROVIDER;
  const isGithub = resolvedProvider === GITHUB_PROVIDER;
  const isCopilot = resolvedProvider === COPILOT_PROVIDER;
  const kind = isCopilot ? "subscription" : form.kind;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(undefined);
    if (catalogError) {
      setError("Provider catalogue is unavailable. Retry loading or use a custom id.");
      return;
    }
    const providerErr = validateProviderId(resolvedProvider);
    if (providerErr) {
      setError(providerErr);
      return;
    }
    let credential: Record<string, unknown>;
    if (isCopilot) {
      const hint = validateCopilotToken(form.repoToken);
      if (hint) {
        setError(hint);
        return;
      }
      credential = { token: form.repoToken.trim() };
    } else if (isGitea) {
      credential = { username: form.repoUsername.trim(), token: form.repoToken };
    } else if (isGithub) {
      credential = { token: form.repoToken };
    } else {
      try {
        credential = JSON.parse(form.credential);
      } catch {
        setError("The credential must be a JSON object.");
        return;
      }
    }
    await action.run(async () => {
      try {
        await call(
          api.POST("/api/accounts", {
            body: {
              name: form.name,
              provider: resolvedProvider,
              kind,
              priority: form.priority,
              max_leases: form.maxLeases ? Number(form.maxLeases) : null,
              credential,
            },
          }),
        );
        set("repoToken", "");
        onDone();
      } catch (err) {
        setError(withoutSecret((err as Error).message, form.repoToken));
        set("repoToken", "");
      }
    });
  }

  return (
    <div className="overlay">
      <form className={`dialog${isCopilot ? " copilot-import" : ""}`} onSubmit={submit}>
        <h2>Add account</h2>
        <p className="muted small">{localBrokerNotice()}</p>
        {catalogError && (
          <p className="error">
            Could not load provider catalogue: {catalogError.message}
            <button type="button" className="link" onClick={() => reloadCatalog()}>
              Retry
            </button>
          </p>
        )}
        <div className="row">
          <label>
            Name
            <input value={form.name} onChange={(e) => set("name", e.target.value)} required />
          </label>
          <label>
            Provider
            <select
              aria-label="Provider"
              value={providerPick}
              onChange={(e) => setProviderPick(e.target.value)}
              disabled={providerOptions.length === 0 && !catalogError}
            >
              {providerOptions.length === 0 && !catalogError && (
                <option value="">Loading catalogue…</option>
              )}
              {providerOptions.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.label}
                </option>
              ))}
              <option value={PROVIDER_PICK_CUSTOM}>Other (custom id)…</option>
            </select>
          </label>
          {providerPick === PROVIDER_PICK_CUSTOM && (
            <label>
              Custom provider id
              <input
                aria-label="Custom provider id"
                value={customProvider}
                onChange={(e) => setCustomProvider(e.target.value)}
                pattern={CUSTOM_PROVIDER_PATTERN}
                maxLength={50}
                required
              />
            </label>
          )}
          <label>
            Kind
            <select
              value={kind}
              disabled={isCopilot}
              onChange={(e) => set("kind", e.target.value as typeof form.kind)}
            >
              <option value="api_key">API key</option>
              <option value="subscription">subscription</option>
            </select>
          </label>
        </div>
        <div className="row">
          <label>
            Priority (higher first)
            <input
              type="number"
              value={form.priority}
              onChange={(e) => set("priority", Number(e.target.value))}
            />
          </label>
          <label>
            Max concurrent leases (empty: unlimited)
            <input
              type="number"
              min={1}
              value={form.maxLeases}
              onChange={(e) => set("maxLeases", e.target.value)}
            />
          </label>
        </div>
        {authNotice && <p className="small muted">{authNotice}</p>}
        {resolvedProvider === "cursor" && (
          <p className="small muted">
            Argus sees a Cursor pool as exhausted only when Cursor refuses work. With on-demand
            spending enabled, Cursor bills instead, so set a spend limit of $0 in Cursor if Argus
            should see exhaustion.
          </p>
        )}
        {isCopilot ? (
          <label>
            Copilot token (stored encrypted, never shown again)
            <input
              type="password"
              aria-label="Copilot token"
              value={form.repoToken}
              onChange={(e) => set("repoToken", e.target.value)}
              autoComplete="off"
              spellCheck={false}
              required
            />
          </label>
        ) : isGitea || isGithub ? (
          <div className="row">
            {isGitea && (
              <label>
                Gitea username
                <input
                  aria-label="Gitea username"
                  value={form.repoUsername}
                  onChange={(e) => set("repoUsername", e.target.value)}
                  autoComplete="off"
                  required
                />
              </label>
            )}
            <label>
              {isGitea ? "Gitea" : "GitHub"} token (stored encrypted, never shown again)
              <input
                type="password"
                aria-label={isGitea ? "Gitea token" : "GitHub token"}
                value={form.repoToken}
                onChange={(e) => set("repoToken", e.target.value)}
                autoComplete="off"
                required
              />
            </label>
          </div>
        ) : (
          <label>
            Credential (JSON; stored encrypted, never shown again)
            <textarea
              rows={5}
              value={form.credential}
              onChange={(e) => set("credential", e.target.value)}
              spellCheck={false}
              autoComplete="off"
            />
          </label>
        )}
        {error && <p className="error">{error}</p>}
        <div className="actions">
          <button type="button" disabled={action.busy} onClick={onClose}>
            Cancel
          </button>
          <ActionButton type="submit" className="primary" pending={action.busy}>
            Add
          </ActionButton>
        </div>
      </form>
    </div>
  );
}
