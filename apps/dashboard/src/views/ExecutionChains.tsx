import type { components } from "@argus/api-client";
import { useEffect, useState } from "react";
import { type Account, api, call } from "../api";
import { useLiveData } from "../live";
import {
  DIFFICULTY_LEVELS,
  type DifficultyLevel,
  putSetting,
  resetSetting,
  type SettingValue,
} from "../settings";

type Capability = components["schemas"]["ExecutionCapability"];
type Selector = components["schemas"]["Selector"];
type Alternative =
  | components["schemas"]["SingleAlternative"]
  | components["schemas"]["PairedAlternative"];
type Policy = { version: 1; difficulties: Partial<Record<DifficultyLevel, Alternative[]>> };
const empty = (): Policy => ({ version: 1, difficulties: {} });
/** No account: "any available account of this provider"; admission picks and records one. */
const blank = (): Selector => ({
  adapter: "claude",
  provider: "local",
  model: { source: "account" },
});

/** Why a capability cannot fill `role` here; undefined when it can. */
function unavailableReason(
  c: Capability,
  role: "harness" | "executor",
  harness?: Selector,
): string | undefined {
  if (
    role === "executor" &&
    harness &&
    (c.excluded_harness_providers ?? []).includes(harness.provider)
  )
    return `a ${harness.provider} harness only pairs with agent or opencode executors`;
  return undefined;
}

/** One line per adapter / provider: which roles it can fill and which modes follow. */
function describeCapabilities(caps: Capability[]) {
  const roles = new Map<string, Set<string>>();
  for (const c of caps) {
    const key = `${c.adapter} / ${c.provider}`;
    roles.set(key, (roles.get(key) ?? new Set()).add(c.role));
  }
  return [...roles.entries()].map(([key, set]) => {
    const harness = set.has("harness");
    return `${key}: ${harness ? "harness or executor" : "executor only"}`;
  });
}

function accountModel(account: Account | undefined, role: "harness" | "executor") {
  return (
    account?.local?.model ??
    account?.config?.[role === "harness" ? "engine" : "executor"]?.model ??
    "Unknown"
  );
}

function SelectorEditor({
  value,
  role,
  capabilities,
  accounts,
  onChange,
  harness,
}: {
  value: Selector;
  role: "harness" | "executor";
  capabilities: Capability[];
  accounts: Account[];
  onChange: (selector: Selector) => void;
  harness?: Selector;
}) {
  const choices = capabilities.filter((c) => c.role === role);
  const capability = choices.find(
    (c) => c.adapter === value.adapter && c.provider === value.provider,
  );
  const account = accounts.find((a) => a.id === value.account_id);
  // A native provider has one gateway identity per session: once the other side
  // is pinned, this side can only use that same account.
  const sharedNative = !!harness && !!capability?.same_provider_same_account;
  return (
    <fieldset>
      <legend>{role === "harness" ? "Harness" : "Executor"}</legend>
      <label>
        Adapter / provider{" "}
        <select
          aria-label={`${role} adapter / provider`}
          value={capability?.id ?? ""}
          onChange={(e) => {
            const selected = choices.find((c) => c.id === e.target.value);
            if (!selected) return;
            onChange({
              adapter: selected.adapter as Selector["adapter"],
              provider: selected.provider as Selector["provider"],
              model: selected.model_sources.includes("account")
                ? { source: "account" }
                : { source: "explicit", id: "" },
            });
          }}
        >
          <option value="" disabled>
            Select a supported adapter
          </option>
          {choices.map((c) => {
            const reason = unavailableReason(c, role, harness);
            return (
              <option key={c.id} value={c.id} disabled={!!reason}>
                {c.adapter} / {c.provider}
                {reason ? ` — ${reason}` : ""}
              </option>
            );
          })}
        </select>
      </label>
      <label>
        Account{" "}
        <select
          aria-label={`${role} account`}
          value={value.account_id ?? ""}
          onChange={(e) => {
            const { account_id: _unpinned, ...rest } = value;
            onChange(e.target.value ? { ...rest, account_id: e.target.value } : rest);
          }}
        >
          <option value="">Any available account</option>
          {accounts
            .filter((a) => a.provider === value.provider)
            .map((a) => {
              const unsupported =
                (a.provider === "custom" &&
                  !a.config?.[role === "harness" ? "engine" : "executor"]?.model) ||
                (sharedNative &&
                  harness.provider === a.provider &&
                  !!harness.account_id &&
                  harness.account_id !== a.id);
              return (
                <option key={a.id} value={a.id} disabled={unsupported}>
                  {a.name} ({a.state}){unsupported ? " — role or same-account restriction" : ""}
                </option>
              );
            })}
        </select>
      </label>
      <p className="small">
        {value.account_id
          ? "Pinned: only this account is used; the alternative is skipped when it cannot serve."
          : "Unpinned: admission leases the best available account of this provider that can serve the role and records which one it chose."}
      </p>
      <label>
        Model source{" "}
        <select
          aria-label={`${role} model source`}
          value={value.model.source}
          onChange={(e) =>
            onChange({
              ...value,
              model:
                e.target.value === "account"
                  ? { source: "account" }
                  : { source: "explicit", id: "" },
            })
          }
        >
          <option value="account" disabled={!capability?.model_sources.includes("account")}>
            Configured account model
          </option>
          <option value="explicit" disabled={!capability?.model_sources.includes("explicit")}>
            Explicit model ID
          </option>
        </select>
      </label>
      {value.model.source === "explicit" && (
        <label>
          Model ID{" "}
          <input
            aria-label={`${role} model ID`}
            value={value.model.id}
            onChange={(e) =>
              onChange({ ...value, model: { source: "explicit", id: e.target.value } })
            }
          />
        </label>
      )}
      <p className="small">
        Configured account model: {accountModel(account, role)}.{" "}
        {value.provider !== "local" && (
          <strong>Paid/cloud alternative; no spend cap implied.</strong>
        )}
      </p>
      {account?.local && (
        <p className="small">
          Discovered: {account.local.served_models?.join(", ") ?? "Unknown"} ·{" "}
          {account.local.discovery.at ?? "Not checked"}. Observed worker model:{" "}
          {account.local.inference.model ?? "Unknown"} ·{" "}
          {account.local.inference.at ?? "Not checked"}.
        </p>
      )}
      {account?.local?.warnings?.map((warning) => (
        <p className="error" key={warning}>
          {warning}
        </p>
      ))}
    </fieldset>
  );
}

/** Whole alternatives only; the scoped draft never materializes inherited lists. */
export function ExecutionChains({
  project,
  setting,
  legacyExecutors,
  legacyNotice,
  onChanged,
}: {
  project: string | null;
  setting?: SettingValue;
  /** Executor order a difficulty uses where no chain exists (built-in default or stored). */
  legacyExecutors: Record<DifficultyLevel, string[]>;
  /** Environment lock on the legacy executor order, shown beside it. */
  legacyNotice?: string;
  onChanged: () => void;
}) {
  const { data: catalog, error: catalogError } = useLiveData(
    (signal) => call(api.GET("/api/engines", { signal })),
    [],
  );
  const { data: accounts, error: accountsError } = useLiveData(
    (signal) => call(api.GET("/api/accounts", { signal })),
    [],
  );
  const [draft, setDraft] = useState<Policy>(
    () => (setting?.scoped_value as Policy | undefined) ?? empty(),
  );
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setDraft((setting?.scoped_value as Policy | undefined) ?? empty());
  }, [project, setting]);
  const effective = (setting?.value as Policy | undefined) ?? empty();
  const caps = catalog?.execution_capabilities ?? [];
  const invalid = Object.values(draft.difficulties).some(
    (list) =>
      !list?.length ||
      list.some(
        (a) =>
          !/^[A-Za-z0-9_-]{1,64}$/.test(a.id) ||
          [a.harness, ...(a.mode === "paired" ? [a.executor] : [])].some(
            (s) => s.model.source === "explicit" && !s.model.id,
          ),
      ),
  );
  function change(level: DifficultyLevel, list: Alternative[] | undefined) {
    const difficulties = { ...draft.difficulties };
    if (list?.length) difficulties[level] = list;
    else delete difficulties[level];
    setDraft({ version: 1, difficulties });
  }
  async function save(reset = false) {
    setBusy(true);
    setError(undefined);
    try {
      if (reset) await resetSetting("execution.chains", project);
      else await putSetting("execution.chains", draft, project);
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="card execution-chains">
      <h3>Execution chains</h3>
      <p>
        Ordered complete alternatives for every difficulty. Single layer has no executor. Fallback
        never mixes accounts between cards. Runtime capabilities describe adapter support, not
        installed CLI, endpoint or model readiness.
      </p>
      <p>
        Task model/account pins bypass the entire chain using legacy_task_pin. Where no chain is
        configured a difficulty keeps its built-in executor order, shown on that level, and the
        model preferences above. Leave the account on <em>Any available account</em> to let
        admission pick one; pin it only to force that account.
      </p>
      <ul className="small" aria-label="Adapter and provider capabilities">
        {describeCapabilities(caps).map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ul>
      {legacyNotice && (
        <p className="error" role="alert">
          {legacyNotice}
        </p>
      )}
      {setting?.notice && (
        <p className="error" role="alert">
          {setting.notice}: explicit chains cannot be saved or admitted while legacy environment
          locks are set.
        </p>
      )}
      {setting?.warnings?.map((warning) => (
        <p className="small" key={warning}>
          {warning}. The alternative stays saved; admission skips it until the account serves it
          again.
        </p>
      ))}
      {(error || catalogError || accountsError) && (
        <p className="error" role="alert">
          {error ?? catalogError?.message ?? accountsError?.message}
        </p>
      )}
      {DIFFICULTY_LEVELS.map((level) => {
        const own = draft.difficulties[level];
        const list = own ?? effective.difficulties[level];
        return (
          <fieldset key={level}>
            <legend>{level}</legend>
            <p>
              Source: {setting?.difficulty_sources?.[level] ?? "legacy"} · revision{" "}
              {setting?.difficulty_revisions?.[level]?.slice(0, 12) ?? "—"}
              {!own && list ? " · inherited (read only until overridden)" : ""}
            </p>
            {!list && (
              <p>
                No chain: built-in routing, harness chosen from the model preferences, executor
                order {legacyExecutors[level].length ? legacyExecutors[level].join(" → ") : "none"}.
                Add an alternative to replace it; a single layer is a harness with no executor.
              </p>
            )}
            {list?.map((alternative, index) => (
              <div className="card" key={`${level}-${index}`}>
                <p>
                  {index + 1}. {alternative.id} ·{" "}
                  {alternative.mode === "single" ? "No executor" : "Harness + executor"}
                </p>
                {!own ? (
                  <p>
                    {alternative.harness.provider} /{" "}
                    {alternative.harness.account_id ?? "any account"}
                    {alternative.mode === "paired"
                      ? ` → ${alternative.executor.provider} / ${alternative.executor.account_id ?? "any account"}`
                      : ""}
                  </p>
                ) : (
                  <>
                    <label>
                      Alternative ID{" "}
                      <input
                        aria-label={`${level} alternative ${index + 1} ID`}
                        value={alternative.id}
                        onChange={(e) =>
                          change(
                            level,
                            own.map((a, i) => (i === index ? { ...a, id: e.target.value } : a)),
                          )
                        }
                      />
                    </label>
                    <label>
                      Mode{" "}
                      <select
                        aria-label={`${level} alternative ${index + 1} mode`}
                        value={alternative.mode}
                        onChange={(e) => {
                          const base = {
                            id: alternative.id,
                            runtime: alternative.runtime,
                            harness: alternative.harness,
                          };
                          change(
                            level,
                            own.map((a, i) =>
                              i !== index
                                ? a
                                : e.target.value === "single"
                                  ? { ...base, mode: "single" }
                                  : {
                                      ...base,
                                      mode: "paired",
                                      executor: { ...blank(), adapter: "opencode" },
                                    },
                            ),
                          );
                        }}
                      >
                        <option value="single">Single layer</option>
                        <option value="paired">Harness + executor</option>
                      </select>
                    </label>
                    <p>Runtime: {alternative.runtime}</p>
                    <SelectorEditor
                      value={alternative.harness}
                      role="harness"
                      capabilities={caps}
                      accounts={accounts ?? []}
                      onChange={(harness) =>
                        change(
                          level,
                          own.map((a, i) => (i === index ? { ...a, harness } : a)),
                        )
                      }
                    />
                    {alternative.mode === "paired" && (
                      <>
                        <SelectorEditor
                          value={alternative.executor}
                          harness={alternative.harness}
                          role="executor"
                          capabilities={caps}
                          accounts={accounts ?? []}
                          onChange={(executor) =>
                            change(
                              level,
                              own.map((a, i) =>
                                i === index && a.mode === "paired" ? { ...a, executor } : a,
                              ),
                            )
                          }
                        />
                        <p className="small">
                          Local harnesses require agent or opencode. Same-provider native pairs run
                          on one account, chosen when the route is admitted. Custom pairs require
                          separate role capacity. Unsupported pairs are rejected on Save.
                        </p>
                      </>
                    )}
                    <button
                      type="button"
                      disabled={index === 0}
                      onClick={() => {
                        const next = [...own];
                        [next[index - 1], next[index]] = [next[index]!, next[index - 1]!];
                        change(level, next);
                      }}
                    >
                      Move up
                    </button>
                    <button
                      type="button"
                      disabled={index === own.length - 1}
                      onClick={() => {
                        const next = [...own];
                        [next[index], next[index + 1]] = [next[index + 1]!, next[index]!];
                        change(level, next);
                      }}
                    >
                      Move down
                    </button>
                    <button
                      type="button"
                      onClick={() =>
                        change(
                          level,
                          own.filter((_, i) => i !== index),
                        )
                      }
                    >
                      Remove alternative
                    </button>
                  </>
                )}
              </div>
            ))}
            {!own && list && (
              <button type="button" onClick={() => change(level, structuredClone(list))}>
                Override {level}
              </button>
            )}
            <button
              type="button"
              disabled={(own?.length ?? 0) >= 16 || !caps.length}
              onClick={() =>
                change(level, [
                  ...(own ?? []),
                  {
                    id: `alternative-${(own?.length ?? 0) + 1}`,
                    mode: "single",
                    runtime: "nanoclaw",
                    harness: blank(),
                  },
                ])
              }
            >
              Add {level} alternative
            </button>
            {own && (
              <button type="button" onClick={() => change(level, undefined)}>
                Inherit / reset {level}
              </button>
            )}
          </fieldset>
        );
      })}
      <button
        type="button"
        disabled={busy || invalid || !caps.length || !!setting?.notice}
        onClick={() => void save()}
      >
        Save execution chains
      </button>
      <button type="button" disabled={busy} onClick={() => void save(true)}>
        Reset execution chains
      </button>
    </section>
  );
}
