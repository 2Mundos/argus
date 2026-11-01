import { type FormEvent, useState } from "react";
import { ActionButton, useAction } from "../ActionButton";
import { api, call } from "../api";
import {
  buildConfig,
  type CustomForm,
  checkBaseUrl,
  emptyForm,
  type RoleForm,
} from "../customAccount";

type Probe = { status: string; reason: string; models?: string[] };

/** Register an OpenAI-compatible endpoint (OpenRouter first) as an account, then check it
 * with `GET {base_url}/models`. After a successful probe the model list fills the harness
 * and executor fields; preferences are PATCHed before Done. The key is sent once and never
 * shown again. */
export function CustomAccount({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState("OpenRouter");
  const [key, setKey] = useState("");
  const [form, setForm] = useState<CustomForm>(emptyForm);
  const [error, setError] = useState<string>();
  const [probe, setProbe] = useState<Probe>();
  const [accountId, setAccountId] = useState<string>();
  const action = useAction();
  const openrouter = form.label.trim() === "openrouter";

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(undefined);
    const urlError = checkBaseUrl(form.baseUrl);
    if (urlError) {
      setError(urlError);
      return;
    }
    await action.run(async () => {
      try {
        const account = await call(
          api.POST("/api/accounts", {
            body: {
              name,
              provider: "custom",
              kind: "api_key",
              credential: { api_key: key.trim() },
              config: buildConfig(form),
            },
          }),
        );
        setKey(""); // the key does not stay in the page
        setAccountId(account.id);
        setProbe(
          await call(
            api.POST("/api/accounts/{account_id}/probe", {
              params: { path: { account_id: account.id } },
            }),
          ),
        );
      } catch (err) {
        setError((err as Error).message);
      }
    });
  }

  async function savePreferences() {
    if (!accountId) return;
    setError(undefined);
    await action.run(async () => {
      try {
        await call(
          api.PATCH("/api/accounts/{account_id}", {
            params: { path: { account_id: accountId } },
            body: { config: buildConfig(form) },
          }),
        );
        onDone();
      } catch (err) {
        setError((err as Error).message);
      }
    });
  }

  const role = (which: "engine" | "executor", title: string) => (
    <RoleFields
      title={title}
      value={form[which]}
      openrouter={openrouter}
      models={probe?.models ?? []}
      onChange={(v) => setForm({ ...form, [which]: v })}
    />
  );

  return (
    <div className="overlay">
      <div className="dialog" role="dialog" aria-label="Add custom provider account">
        <h2>Add custom provider account</h2>
        <p className="muted small">
          Any OpenAI-compatible endpoint. The key is stored encrypted as an Argus account and is
          never shown again.
        </p>
        {probe && accountId ? (
          <>
            <p role="status">
              Saved. Check: <strong>{probe.status}</strong> — {probe.reason}
              {probe.models?.length ? ` (${probe.models.length} models)` : ""}
            </p>
            <p className="muted small">
              Pick a model for the NanoClaw harness (and optionally the code executor). Suggestions
              come from the provider catalogue when the check succeeded.
            </p>
            {role("engine", "NanoClaw harness")}
            {role("executor", "Code executor")}
            {error && (
              <p role="alert" className="error">
                {error}
              </p>
            )}
            <div className="row">
              <ActionButton
                type="button"
                className="primary"
                pending={action.busy}
                pendingLabel="Saving…"
                action={() => void savePreferences()}
              >
                Save preferences
              </ActionButton>
              <button type="button" disabled={action.busy} onClick={onDone}>
                Skip
              </button>
            </div>
          </>
        ) : (
          <form onSubmit={submit}>
            <label>
              Name
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
                maxLength={100}
              />
            </label>
            <label>
              Base URL
              <input
                value={form.baseUrl}
                onChange={(e) => setForm({ ...form, baseUrl: e.target.value })}
                required
              />
            </label>
            <label>
              Provider label
              <input
                value={form.label}
                onChange={(e) => setForm({ ...form, label: e.target.value })}
                required
                pattern="[a-z0-9][a-z0-9_\-]{0,49}"
              />
            </label>
            <label>
              API key
              <input
                type="password"
                autoComplete="off"
                value={key}
                onChange={(e) => setKey(e.target.value)}
                required
              />
            </label>
            <p className="muted small">
              After the account is checked you can pick harness and executor models from the
              provider list.
            </p>
            {error && (
              <p role="alert" className="error">
                {error}
              </p>
            )}
            <div className="row">
              <ActionButton
                type="submit"
                className="primary"
                pending={action.busy}
                pendingLabel="Saving…"
              >
                Save and check
              </ActionButton>
              <button type="button" onClick={onClose}>
                Cancel
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}

function RoleFields({
  title,
  value,
  openrouter,
  models,
  onChange,
}: {
  title: string;
  value: RoleForm;
  openrouter: boolean;
  models: string[];
  onChange: (v: RoleForm) => void;
}) {
  const set = (patch: Partial<RoleForm>) => onChange({ ...value, ...patch });
  const list = `models-${title.replace(/\W/g, "")}`;
  return (
    <fieldset>
      <legend>{title}</legend>
      <label>
        Model
        <input
          value={value.model}
          list={list}
          onChange={(e) => set({ model: e.target.value })}
          placeholder="author/slug"
        />
        <datalist id={list}>
          {models.map((m) => (
            <option key={m} value={m} />
          ))}
        </datalist>
      </label>
      {openrouter && (
        <>
          <label>
            Provider order (comma separated)
            <input value={value.order} onChange={(e) => set({ order: e.target.value })} />
          </label>
          <label>
            Only these providers
            <input value={value.only} onChange={(e) => set({ only: e.target.value })} />
          </label>
          <label>
            Quantizations
            <input
              value={value.quantizations}
              onChange={(e) => set({ quantizations: e.target.value })}
            />
          </label>
          <label>
            Fallbacks
            <select
              value={value.fallbacks}
              onChange={(e) => set({ fallbacks: e.target.value as RoleForm["fallbacks"] })}
            >
              <option value="default">OpenRouter default</option>
              <option value="allow">Allow</option>
              <option value="deny">Deny</option>
            </select>
          </label>
        </>
      )}
    </fieldset>
  );
}
