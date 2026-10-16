import { type FormEvent, useEffect, useState } from "react";
import {
  CUSTOM_PROVIDER_PATTERN,
  localBrokerNotice,
  nativeAuthNotice,
  PROVIDER_PICK_CUSTOM,
  providerOptionsFromCatalog,
  validateProviderId,
} from "../accountProviders";
import { type Account, api, call } from "../api";
import { relative } from "../format";
import { useLiveData } from "../live";
import { ClaudeAccount } from "./ClaudeAccount";
import { CodexAccount } from "./CodexAccount";

export function Accounts() {
  const [adding, setAdding] = useState(false);
  const [addingCodex, setAddingCodex] = useState(false);
  // A Claude wizard: new (`{}`) or renewing the given account.
  const [claude, setClaude] = useState<{ renew?: Account }>();
  const [notice, setNotice] = useState<string>();
  const [error, setError] = useState<string>();
  const { data: accounts, error: loadError } = useLiveData(
    () => call(api.GET("/api/accounts")),
    [],
  );

  async function toggle(account: Account) {
    setError(undefined);
    try {
      await call(
        api.PATCH("/api/accounts/{account_id}", {
          params: { path: { account_id: account.id } },
          body: { enabled: account.state === "disabled" },
        }),
      );
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
      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Provider</th>
            <th>State</th>
            <th>Priority</th>
            <th>Leases</th>
            <th>Last 24 h</th>
            <th>Total</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {(accounts ?? []).map((a) => (
            <tr key={a.id}>
              <td>
                {a.name} <span className="muted small">{a.kind.replace("_", " ")}</span>
              </td>
              <td>{a.provider}</td>
              <td>
                <span className={`badge account-${a.state}`}>{a.state}</span>
                {a.state === "exhausted" && (
                  <span className="muted small"> resets {relative(a.exhausted_until)}</span>
                )}
                {a.disabled_reason && <span className="muted small"> {a.disabled_reason}</span>}
              </td>
              <td>{a.priority}</td>
              <td>
                {a.active_leases}
                {a.max_leases != null && ` / ${a.max_leases}`}
              </td>
              <td className="small">{usage(a.usage_24h)}</td>
              <td className="small">{usage(a.usage_total)}</td>
              <td>
                <button type="button" className="link" onClick={() => toggle(a)}>
                  {a.state === "disabled" ? "Enable" : "Disable"}
                </button>
                {a.provider === "anthropic" && (
                  <button
                    type="button"
                    className="link"
                    aria-label={`Renew credential of ${a.name}`}
                    onClick={() => setClaude({ renew: a })}
                  >
                    Renew credential
                  </button>
                )}
              </td>
            </tr>
          ))}
          {accounts?.length === 0 && (
            <tr>
              <td colSpan={8} className="muted">
                No accounts yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
      {adding && <AddAccount onClose={() => setAdding(false)} />}
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

function usage(u: Account["usage_24h"]): string {
  const tokens = u.input_tokens + u.output_tokens;
  if (tokens === 0 && u.cost_usd === 0) return "—";
  const k = tokens >= 1000 ? `${(tokens / 1000).toFixed(1)}k` : String(tokens);
  return `${k} tokens · $${u.cost_usd.toFixed(2)}`;
}

function AddAccount({ onClose }: { onClose: () => void }) {
  const {
    data: catalog,
    error: catalogError,
    reload: reloadCatalog,
  } = useLiveData(() => call(api.GET("/api/engines")), []);
  const providerOptions = catalog ? providerOptionsFromCatalog(catalog) : [];
  const [providerPick, setProviderPick] = useState("");
  const [customProvider, setCustomProvider] = useState("");
  const [form, setForm] = useState({
    name: "",
    kind: "api_key" as "api_key" | "subscription",
    priority: 0,
    maxLeases: "",
    credential: '{\n  "api_key": ""\n}',
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
    try {
      credential = JSON.parse(form.credential);
    } catch {
      setError("The credential must be a JSON object.");
      return;
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
