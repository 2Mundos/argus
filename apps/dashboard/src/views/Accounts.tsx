import { type FormEvent, useEffect, useState } from "react";
import { ActionButton, useAction } from "../ActionButton";
import { AuthFacts } from "../AuthFacts";
import {
  COPILOT_PROVIDER,
  CUSTOM_PROVIDER_PATTERN,
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
import type { CustomPresetId } from "../customAccount";
import { DateChip } from "../DateChip";
import { useLiveData } from "../live";
import { PageHeader } from "../PageHeader";
import {
  bucketTokens,
  hasModelUsage,
  NOT_MEASURED,
  notMeasured,
  reasonsNote,
  roleSplit,
  tokens,
  totalTokens,
  unmeasuredNote,
  viewerTimeZone,
  windowQuery,
} from "../usage";
import { AccountDetail } from "./AccountDetail";
import { AddAccountForm, DeleteAccount, EditAccount, ReplaceCredential } from "./AccountDialogs";
import { ClaudeAccount } from "./ClaudeAccount";
import { CodexAccount } from "./CodexAccount";
import { CustomAccount } from "./CustomAccount";
import { AccountQuota, Sparkline } from "./charts";
import { LocalAccount } from "./LocalAccount";
import { LocalEndpoints } from "./LocalEndpoints";
import { ProviderMark, StateIcon } from "./providerIcons";

type AccountType = "claude" | "codex" | "google" | "custom" | "local" | "other";

const ACCOUNT_TYPES: { id: AccountType; title: string; hint: string }[] = [
  { id: "claude", title: "Claude", hint: "Subscription login" },
  { id: "codex", title: "Codex", hint: "ChatGPT login or API key" },
  { id: "google", title: "Google AI Studio", hint: "Gemini API key" },
  { id: "custom", title: "Custom provider", hint: "OpenRouter or any compatible API" },
  { id: "local", title: "Local model", hint: "Ollama, LM Studio, vLLM…" },
  { id: "other", title: "Other", hint: "Cursor, Copilot, Gitea, GitHub, raw JSON" },
];

/** Wizard step 1: choose the kind of account, then hand off to its own dialog. */
function AddAccountTypePicker(props: { onPick: (type: AccountType) => void; onClose: () => void }) {
  const { onClose } = props;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="overlay">
      <div className="dialog account-type-picker" role="dialog" aria-label="Add account">
        <h2>Add account</h2>
        <p className="muted small">What kind of account?</p>
        <div className="account-type-grid">
          {ACCOUNT_TYPES.map((t) => (
            <button
              key={t.id}
              type="button"
              className="compact"
              aria-label={t.title}
              onClick={() => props.onPick(t.id)}
            >
              <strong>{t.title}</strong>
              <span className="muted small">{t.hint}</span>
            </button>
          ))}
        </div>
        <div className="actions">
          <button type="button" className="compact" onClick={props.onClose}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

export function Accounts() {
  const [picking, setPicking] = useState(false);
  const [adding, setAdding] = useState(false);
  const [addingCodex, setAddingCodex] = useState(false);
  const [addingCustom, setAddingCustom] = useState<CustomPresetId>();
  const [addingLocal, setAddingLocal] = useState(false);
  // A Claude wizard: new (`{}`) or renewing the given account.
  const [claude, setClaude] = useState<{ renew?: Account }>();
  const [editing, setEditing] = useState<Account>();
  const [deleting, setDeleting] = useState<Account>();
  const [replacing, setReplacing] = useState<Account>();
  const [endpoints, setEndpoints] = useState<Account>();
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
  const action = useAction();

  async function toggle(account: Account) {
    setError(undefined);
    await action.run(async () => {
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
    }, `toggle:${account.id}`);
  }

  async function reload() {
    setError(undefined);
    await action.run(async () => {
      try {
        const result = await call(api.POST("/api/admin/secrets/reload"));
        setNotice(`Reloaded the secrets file: ${result.secrets} entries.`);
      } catch (err) {
        setError((err as Error).message);
      }
    }, "reload-secrets");
  }

  return (
    <section>
      <PageHeader
        title="Accounts"
        lead="Provider accounts agents lease. Credentials live encrypted in the SOPS file and are never shown here. When an account hits its quota, agents fall back to the next one."
      >
        <ActionButton
          pending={action.is("reload-secrets")}
          className="compact"
          disabled={action.busy}
          action={() => reload()}
        >
          Reload secrets file
        </ActionButton>
        <button type="button" className="primary compact" onClick={() => setPicking(true)}>
          Add account
        </button>
      </PageHeader>
      {notice && <p className="small">{notice}</p>}
      {(error || loadError) && <p className="error">{error ?? loadError?.message}</p>}
      <table className="accounts-table accounts">
        <thead>
          <tr>
            <th>Account</th>
            <th>State</th>
            <th>Auth</th>
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
                    <span className="muted small">
                      {" "}
                      resets <DateChip at={a.exhausted_until} />
                    </span>
                  )}
                  {a.disabled_reason && <span className="muted small"> {a.disabled_reason}</span>}
                  {(a.local?.warnings ?? []).length > 0 && (
                    <span className="small error" title={(a.local?.warnings ?? []).join("\n")}>
                      {" "}
                      stale model
                    </span>
                  )}
                </td>
                <td data-label="Auth">
                  <AuthFacts account={a} />
                </td>
                <td data-label="Quota left">
                  {!model ? (
                    <span className="muted small">—</span>
                  ) : (
                    <AccountQuota quota={a.quota} status={a.quota_status} provider={a.provider} />
                  )}
                </td>
                <td data-label="Tokens, 7 days">
                  {!model ? (
                    <span className="muted small">—</span>
                  ) : series?.reported ? (
                    <span className="spark-cell">
                      {notMeasured(series.totals) ? (
                        <span
                          className="muted small"
                          title={reasonsNote(series.totals.unmeasured_reasons) ?? "no reason given"}
                        >
                          {NOT_MEASURED}
                        </span>
                      ) : (
                        <>
                          <Sparkline
                            values={series.buckets.map(bucketTokens)}
                            label={`${a.name}: tokens per day, last 7 days`}
                          />
                          <span className="small">{tokens(totalTokens(series.totals))}</span>
                        </>
                      )}
                      {unmeasuredNote(series.totals.unmeasured_runs) && (
                        <span
                          className="muted small"
                          title={reasonsNote(series.totals.unmeasured_reasons) ?? undefined}
                        >
                          {unmeasuredNote(series.totals.unmeasured_runs)}
                        </span>
                      )}
                      {roleSplit(series.totals.runs_by_role) && (
                        <span className="muted small">{roleSplit(series.totals.runs_by_role)}</span>
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
                    {a.provider === "local" && (
                      <button
                        type="button"
                        className="link"
                        aria-label={`Local endpoints of ${a.name}`}
                        onClick={() => setEndpoints(a)}
                      >
                        Endpoints
                      </button>
                    )}
                    <ActionButton
                      className="link"
                      aria-label={`${a.state === "disabled" ? "Enable" : "Disable"} ${a.name}`}
                      pending={action.is(`toggle:${a.id}`)}
                      disabled={action.busy}
                      action={() => toggle(a)}
                    >
                      {a.state === "disabled" ? "Enable" : "Disable"}
                    </ActionButton>
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
              <td colSpan={7} className="muted">
                No accounts yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
      {picking && (
        <AddAccountTypePicker
          onClose={() => setPicking(false)}
          onPick={(type) => {
            setPicking(false);
            if (type === "claude") setClaude({});
            else if (type === "codex") setAddingCodex(true);
            else if (type === "google") setAddingCustom("google");
            else if (type === "custom") setAddingCustom("openrouter");
            else if (type === "local") setAddingLocal(true);
            else setAdding(true);
          }}
        />
      )}
      {adding && (
        <AddAccountForm
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
      {endpoints && (
        <LocalEndpoints
          account={endpoints}
          onClose={() => setEndpoints(undefined)}
          onSaved={(message) => {
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
        <AccountDetail
          account={selectedAccount}
          onClose={() => setSelected(undefined)}
          onRefreshed={reloadList}
        />
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
          preset={addingCustom}
          onClose={() => setAddingCustom(undefined)}
          onDone={() => {
            const google = addingCustom === "google";
            setAddingCustom(undefined);
            setNotice(
              google ? "Google AI Studio account added." : "Custom provider account added.",
            );
            reloadList();
          }}
        />
      )}
      {addingLocal && (
        <LocalAccount
          onClose={() => setAddingLocal(false)}
          onDone={() => {
            setAddingLocal(false);
            setNotice("Local model account added.");
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
