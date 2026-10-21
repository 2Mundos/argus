import { type FormEvent, useEffect, useState } from "react";
import {
  CUSTOM_PROVIDER_PATTERN,
  GITEA_PROVIDER,
  localBrokerNotice,
  nativeAuthNotice,
  PROVIDER_PICK_CUSTOM,
  providerOptionsFromCatalog,
  validateProviderId,
} from "../accountProviders";
import { type Account, api, call } from "../api";
import { relative } from "../format";
import { useLiveData } from "../live";
import {
  allUnmeasured,
  hasModelUsage,
  tokens,
  totalTokens,
  unmeasuredNote,
  viewerTimeZone,
  windowQuery,
} from "../usage";
import { AccountDetail } from "./AccountDetail";
import { DeleteAccount, EditAccount, ReplaceCredential } from "./AccountDialogs";
import { ClaudeAccount } from "./ClaudeAccount";
import { CodexAccount } from "./CodexAccount";
import { CustomAccount } from "./CustomAccount";
import { AccountQuota, Sparkline } from "./charts";
import { ProviderMark, StateIcon } from "./providerIcons";

export function Accounts() {
  const [adding, setAdding] = useState(false);
  const [addingCodex, setAddingCodex] = useState(false);
  const [addingCustom, setAddingCustom] = useState(false);
  // A Claude wizard: new (`{}`) or renewing the given account.
  const [claude, setClaude] = useState<{ renew?: Account }>();
  const [editing, setEditing] = useState<Account>();
  const [deleting, setDeleting] = useState<Account>();
  const [replacing, setReplacing] = useState<Account>();
  const [notice, setNotice] = useState<string>();
  const [error, setError] = useState<string>();
  const {
    data: accounts,
    error: loadError,
    reload: reloadList,
  } = useLiveData((signal) => call(api.GET("/api/accounts", { signal })), []);
  const { data: week } = useLiveData(
    (signal) =>
      call(
        api.GET("/api/accounts/usage", {
          params: { query: { ...windowQuery("7d", new Date()), tz: viewerTimeZone() } },
          signal,
        }),
      ),
    [],
  );
  const [selected, setSelected] = useState<string>();
  const selectedAccount = accounts?.find((a) => a.id === selected);

  async function toggle(account: Account) {
    setError(undefined);
    try {
      await call(
        api.PATCH("/api/accounts/{account_id}", {
          params: { path: { account_id: account.id } },
          body: { enabled: account.state === "disabled" },
        }),
      );
      reloadList();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function reload() {
    setError(undefined);
    try {
      const result = await call(api.POST("/api/admin/secrets/reload"));
      setNotice(`Reloaded the secrets file: ${result.secrets} entries.`);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <section>
      <div className="toolbar">
        <h2>Accounts</h2>
        <button type="button" onClick={reload}>
          Reload secrets file
        </button>
        <button type="button" onClick={() => setAddingCodex(true)}>
          Add Codex account
        </button>
        <button type="button" onClick={() => setClaude({})}>
          Add Claude account
        </button>
        <button type="button" onClick={() => setAddingCustom(true)}>
          Add custom provider
        </button>
        <button type="button" className="primary" onClick={() => setAdding(true)}>
          Add account
        </button>
      </div>
      <p className="muted small">
        Provider accounts agents lease. Credentials live encrypted in the SOPS file and are never
        shown here. When an account hits its quota, agents fall back to the next one.
      </p>
      {notice && <p className="small">{notice}</p>}
      {(error || loadError) && <p className="error">{error ?? loadError?.message}</p>}
      <table className="accounts-table accounts">
        <thead>
          <tr>
            <th>Account</th>
            <th>State</th>
            <th>Quota left</th>
            <th>Tokens, 7 days</th>
            <th>Leases</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {(accounts ?? []).map((a) => {
            const series = week?.accounts.find((u) => u.account_id === a.id);
            const model = hasModelUsage(a.provider);
            return (
              <tr key={a.id}>
                <td data-label="Account">
                  <button
                    type="button"
                    className="link account-name"
                    onClick={() => setSelected(a.id)}
                  >
                    <ProviderMark provider={a.provider} /> {a.name}
                  </button>{" "}
                  <span className="muted small">
                    {a.provider} · {a.kind.replace("_", " ")} · priority {a.priority}
                  </span>
                </td>
                <td data-label="State">
                  <span className={`badge account-${a.state}`}>
                    <StateIcon state={a.state} /> {a.state}
                  </span>
                  {a.state === "exhausted" && (
                    <span className="muted small"> resets {relative(a.exhausted_until)}</span>
                  )}
                  {a.disabled_reason && <span className="muted small"> {a.disabled_reason}</span>}
                </td>
                <td data-label="Quota left">
                  {!model ? (
                    <span className="muted small">—</span>
                  ) : (
                    <AccountQuota quota={a.quota} status={a.quota_status} />
                  )}
                </td>
                <td data-label="Tokens, 7 days">
                  {!model ? (
                    <span className="muted small">—</span>
                  ) : series?.reported ? (
                    <span className="spark-cell">
                      {!allUnmeasured(series.totals) && (
                        <>
                          <Sparkline
                            values={series.buckets.map(totalTokens)}
                            label={`${a.name}: tokens per day, last 7 days`}
                          />
                          <span className="small">{tokens(totalTokens(series.totals))}</span>
                        </>
                      )}
                      {unmeasuredNote(series.totals.unmeasured_runs) && (
                        <span className="muted small">
                          {unmeasuredNote(series.totals.unmeasured_runs)}
                        </span>
                      )}
                    </span>
                  ) : (
                    <span className="muted small">not reported</span>
                  )}
                </td>
                <td data-label="Leases">
                  {a.active_leases}
                  {a.max_leases != null && ` / ${a.max_leases}`}
                </td>
                <td>
                  <div className="account-actions">
                    <button
                      type="button"
                      className="link"
                      aria-label={`Edit ${a.name}`}
                      onClick={() => setEditing(a)}
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      className="link"
                      aria-label={`${a.state === "disabled" ? "Enable" : "Disable"} ${a.name}`}
                      onClick={() => toggle(a)}
                    >
                      {a.state === "disabled" ? "Enable" : "Disable"}
                    </button>
                    {a.provider === "anthropic" ? (
                      <button
                        type="button"
                        className="link"
                        aria-label={`Renew credential of ${a.name}`}
                        onClick={() => setClaude({ renew: a })}
                      >
                        Renew credential
                      </button>
                    ) : (
                      <button
                        type="button"
                        className="link"
                        aria-label={`Replace credential of ${a.name}`}
                        onClick={() => setReplacing(a)}
                      >
                        Replace credential
                      </button>
                    )}
                    <button
                      type="button"
                      className="link danger"
                      aria-label={`Delete ${a.name}`}
                      onClick={() => setDeleting(a)}
                    >
                      Delete
                    </button>
                  </div>
                </td>
              </tr>
            );
          })}
          {accounts?.length === 0 && (
            <tr>
              <td colSpan={6} className="muted">
                No accounts yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
      {adding && (
        <AddAccount
          onClose={() => {
            setAdding(false);
            reloadList();
          }}
        />
      )}
      {editing && (
        <EditAccount
          account={editing}
          onClose={() => setEditing(undefined)}
          onDone={(message) => {
            setEditing(undefined);
            setNotice(message);
            reloadList();
          }}
        />
      )}
      {replacing && (
        <ReplaceCredential
          account={replacing}
          onClose={() => setReplacing(undefined)}
          onDone={(message) => {
            setReplacing(undefined);
            setNotice(message);
            reloadList();
          }}
        />
      )}
      {deleting && (
        <DeleteAccount
          account={deleting}
          onClose={() => setDeleting(undefined)}
          onDone={(message) => {
            setDeleting(undefined);
            setNotice(message);
            reloadList();
          }}
        />
      )}
      {selectedAccount && (
        <AccountDetail account={selectedAccount} onClose={() => setSelected(undefined)} />
      )}
      {claude && (
        <ClaudeAccount
          renew={claude.renew}
          onClose={() => setClaude(undefined)}
          onDone={() => {
            setNotice(claude.renew ? `Renewed “${claude.renew.name}”.` : "Claude account added.");
            setClaude(undefined);
          }}
        />
      )}
      {addingCustom && (
        <CustomAccount
          onClose={() => setAddingCustom(false)}
          onDone={() => {
            setAddingCustom(false);
            setNotice("Custom provider account added.");
            reloadList();
          }}
        />
      )}
      {addingCodex && (
        <CodexAccount
          onClose={() => setAddingCodex(false)}
          onDone={() => {
            setAddingCodex(false);
            setNotice("Codex account added.");
          }}
        />
      )}
    </section>
  );
}

function AddAccount({ onClose }: { onClose: () => void }) {
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
    giteaUsername: "",
    giteaToken: "",
  });
  const [error, setError] = useState<string>();
  const set = <K extends keyof typeof form>(key: K, value: (typeof form)[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  useEffect(() => {
    if (providerPick || providerOptions.length === 0) return;
    const first = providerOptions[0];
    if (first) setProviderPick(first.id);
  }, [providerOptions, providerPick]);

  const resolvedProvider =
    providerPick === PROVIDER_PICK_CUSTOM ? customProvider.trim() : providerPick;
  const authNotice = nativeAuthNotice(resolvedProvider);
  const isGitea = resolvedProvider === GITEA_PROVIDER;

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
    if (isGitea) {
      credential = { username: form.giteaUsername.trim(), token: form.giteaToken };
    } else {
      try {
        credential = JSON.parse(form.credential);
      } catch {
        setError("The credential must be a JSON object.");
        return;
      }
    }
    try {
      await call(
        api.POST("/api/accounts", {
          body: {
            name: form.name,
            provider: resolvedProvider,
            kind: form.kind,
            priority: form.priority,
            max_leases: form.maxLeases ? Number(form.maxLeases) : null,
            credential,
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
              value={form.kind}
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
        {isGitea ? (
          <div className="row">
            <label>
              Gitea username
              <input
                aria-label="Gitea username"
                value={form.giteaUsername}
                onChange={(e) => set("giteaUsername", e.target.value)}
                autoComplete="off"
                required
              />
            </label>
            <label>
              Gitea token (stored encrypted, never shown again)
              <input
                type="password"
                aria-label="Gitea token"
                value={form.giteaToken}
                onChange={(e) => set("giteaToken", e.target.value)}
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
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="primary">
            Add
          </button>
        </div>
      </form>
    </div>
  );
}
