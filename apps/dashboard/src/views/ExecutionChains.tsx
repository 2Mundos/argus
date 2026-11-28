import type { components } from "@argus/api-client";
import { type ReactNode, useEffect, useId, useRef, useState } from "react";
import { type Account, api, call } from "../api";
import { useLiveData } from "../live";
import {
  DIFFICULTY_LEVELS,
  type DifficultyLevel,
  putSetting,
  resetSetting,
  type SettingValue,
} from "../settings";
import { useUnsavedChanges } from "../unsaved";

type Capability = components["schemas"]["ExecutionCapability"];
type Selector = components["schemas"]["Selector"];
type Alternative =
  | components["schemas"]["SingleAlternative"]
  | components["schemas"]["PairedAlternative"];
type Policy = { version: 1; difficulties: Partial<Record<DifficultyLevel, Alternative[]>> };
const empty = (): Policy => ({ version: 1, difficulties: {} });
const policyOf = (setting?: SettingValue): Policy =>
  (setting?.scoped_value as Policy | undefined) ?? empty();
const ALT_ID = /^[A-Za-z0-9_-]{1,64}$/;
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

/**
 * Reasoning effort levels this selector takes (execution-chains spec 3.8): the
 * capability's levels, narrowed by the first model rule an explicit model matches.
 * `verified` is false when the model is listed by no rule and the CLI's model check
 * was not verified.
 */
function effortChoices(c: Capability | undefined, selector: Selector) {
  const levels = c?.efforts ?? [];
  if (selector.model.source !== "explicit") return { levels, verified: true };
  const model = selector.model.id.toLowerCase().replace(/-\d{8}$/, "");
  const rule = c?.effort_model_rules?.find(
    (r) => r.models?.includes(model) || r.prefixes?.some((p) => model.startsWith(p)),
  );
  if (rule) return { levels: levels.filter((l) => rule.efforts.includes(l)), verified: true };
  return { levels, verified: c?.effort_unlisted_models !== "unverified" };
}

/** Why the selector's effort cannot be saved; undefined when it can (or is omitted). */
function effortProblem(caps: Capability[], selector: Selector, role: "harness" | "executor") {
  if (!selector.effort) return undefined;
  const c = caps.find(
    (x) => x.role === role && x.adapter === selector.adapter && x.provider === selector.provider,
  );
  if (!c?.efforts?.length)
    return `${role} effort: ${selector.adapter} / ${selector.provider} has no reasoning effort setting`;
  if (!effortChoices(c, selector).levels.includes(selector.effort))
    return `${role} effort: ${selector.effort} is not supported by this ${selector.adapter} model`;
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

/** Every reason the draft cannot be saved, one line each; empty when it is valid. */
function draftProblems(draft: Policy, caps: Capability[]): string[] {
  const problems: string[] = [];
  for (const level of DIFFICULTY_LEVELS) {
    const list = draft.difficulties[level];
    if (!list) continue;
    if (!list.length) problems.push(`${level}: list is empty`);
    list.forEach((a, index) => {
      const where = `${level}, alternative ${index + 1}`;
      if (!ALT_ID.test(a.id))
        problems.push(`${where}: alternative ID must match [A-Za-z0-9_-]{1,64}`);
      if (a.harness.model.source === "explicit" && !a.harness.model.id)
        problems.push(`${where}: harness model ID is empty`);
      if (a.mode === "paired" && a.executor.model.source === "explicit" && !a.executor.model.id)
        problems.push(`${where}: executor model ID is empty`);
      for (const problem of [
        effortProblem(caps, a.harness, "harness"),
        a.mode === "paired" ? effortProblem(caps, a.executor, "executor") : undefined,
      ])
        if (problem) problems.push(`${where}: ${problem}`);
    });
  }
  return problems;
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
  const effort = effortChoices(capability, value);
  const accountHint = value.account_id
    ? "Pinned: only this account; skipped when it cannot serve."
    : "Unpinned: admission leases a matching account and records which one.";
  const effortHint = capability?.efforts?.length
    ? `${capability.effort_interface}${effort.verified ? "" : " · model not on verified list"}`
    : `${value.adapter} / ${value.provider}: no effort setting; runtime default.`;
  return (
    <fieldset className="chain-role">
      <legend>{role === "harness" ? "Harness" : "Executor"}</legend>
      <div className="chain-fields">
        <label>
          Adapter / provider
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
          Account
          <select
            aria-label={`${role} account`}
            title={accountHint}
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
        <label>
          Model source
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
        {value.model.source === "explicit" ? (
          <label>
            Model ID
            <input
              aria-label={`${role} model ID`}
              value={value.model.id}
              onChange={(e) =>
                onChange({ ...value, model: { source: "explicit", id: e.target.value } })
              }
            />
          </label>
        ) : (
          <label>
            Account model
            <input
              readOnly
              value={accountModel(account, role)}
              aria-label={`${role} configured account model`}
            />
          </label>
        )}
        <label>
          Reasoning effort
          <select
            aria-label={`${role} reasoning effort`}
            title={effortHint}
            value={value.effort ?? ""}
            disabled={!capability?.efforts?.length && !value.effort}
            onChange={(e) => {
              const { effort: _omitted, ...rest } = value;
              onChange(e.target.value ? { ...rest, effort: e.target.value } : rest);
            }}
          >
            <option value="">Runtime default</option>
            {effort.levels.map((level) => (
              <option key={level} value={level}>
                {level}
              </option>
            ))}
            {value.effort && !effort.levels.includes(value.effort) && (
              <option value={value.effort} disabled>
                {value.effort} (not supported here)
              </option>
            )}
          </select>
        </label>
      </div>
      <p className="muted small chain-role-meta">
        {accountHint} · {effortHint}
        {value.provider !== "local" ? " · Paid/cloud; no spend cap implied." : ""}
        {account?.local
          ? ` · Discovered ${account.local.served_models?.join(", ") ?? "Unknown"} (${account.local.discovery.at ?? "Not checked"}); observed ${account.local.inference.model ?? "Unknown"} (${account.local.inference.at ?? "Not checked"}).`
          : ""}
      </p>
      {account?.local?.warnings?.map((warning) => (
        <p className="error small" key={warning}>
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
  settingKey = "execution.chains",
  title = "Execution chains",
  intro,
  unconfigured,
  legacyExecutors,
  legacyNotice,
  onChanged,
}: {
  project: string | null;
  setting?: SettingValue;
  /** Another key with the same shape as `execution.chains`. */
  settingKey?: string;
  title?: string;
  /** Replaces the execution-chain explanation. */
  intro?: ReactNode;
  /** What a level without a chain does; defaults to the built-in executor order. */
  unconfigured?: string;
  /** Executor order a difficulty uses where no chain exists (built-in default or stored). */
  legacyExecutors?: Record<DifficultyLevel, string[]>;
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
  const [draft, setDraft] = useState<Policy>(() => policyOf(setting));
  const [baseline, setBaseline] = useState<Policy>(() => policyOf(setting));
  const [level, setLevel] = useState<DifficultyLevel>("trivial");
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const dirty = JSON.stringify(draft) !== JSON.stringify(baseline);
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;
  const projectRef = useRef(project);
  useUnsavedChanges(dirty);
  // Live/reload refreshes replace `setting` identity; keep unsaved edits until save or scope change.
  useEffect(() => {
    const next = policyOf(setting);
    const scopeChanged = projectRef.current !== project;
    projectRef.current = project;
    setBaseline(next);
    if (scopeChanged || !dirtyRef.current) setDraft(next);
  }, [project, setting]);
  const effective = (setting?.value as Policy | undefined) ?? empty();
  const caps = catalog?.execution_capabilities ?? [];
  const notesId = useId();
  const problems = draftProblems(draft, caps);
  const notes = [
    ...problems,
    ...(caps.length ? [] : ["No execution capabilities loaded from /api/engines"]),
    ...(setting?.notice
      ? [
          `${setting.notice}: explicit chains cannot be saved or admitted while legacy environment locks are set.`,
        ]
      : []),
    ...(busy ? ["Saving…"] : []),
  ];
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
      if (reset) {
        await resetSetting(settingKey, project);
        const cleared = empty();
        setDraft(cleared);
        setBaseline(cleared);
      } else {
        await putSetting(settingKey, draft, project);
        setBaseline(draft);
      }
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="card execution-chains">
      <div className="toolbar">
        <h3>{title}</h3>
        <button
          type="button"
          aria-describedby={notes.length > 0 ? notesId : undefined}
          title={notes[0]}
          disabled={busy || problems.length > 0 || !caps.length || !!setting?.notice}
          onClick={() => void save()}
        >
          Save {title.toLowerCase()}
        </button>
        <button type="button" disabled={busy} onClick={() => void save(true)}>
          Reset {title.toLowerCase()}
        </button>
      </div>
      {notes.length > 0 && (
        <ul
          id={notesId}
          role="status"
          className={`small chain-notes${problems.length ? " error" : ""}`}
        >
          {notes.map((note) => {
            const jump = DIFFICULTY_LEVELS.find(
              (d) => note.startsWith(`${d}:`) || note.startsWith(`${d},`),
            );
            return (
              <li key={note}>
                {jump ? (
                  <button type="button" className="link" onClick={() => setLevel(jump)}>
                    {note}
                  </button>
                ) : (
                  note
                )}
              </li>
            );
          })}
        </ul>
      )}
      {intro ?? (
        <p className="muted small">
          Ordered fallbacks per difficulty. Single layer has no executor; pins bypass the chain.
          Leave account on Any available unless you need a pin. Alternative IDs are letters, digits,
          `_` and `-` only (no spaces).
        </p>
      )}
      {caps.length > 0 && (
        <details className="chain-caps">
          <summary className="small">Adapter capabilities ({caps.length})</summary>
          <ul className="small" aria-label="Adapter and provider capabilities">
            {describeCapabilities(caps).map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </details>
      )}
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
      <nav className="subtabs" role="tablist" aria-label="Difficulty levels">
        {DIFFICULTY_LEVELS.map((id) => {
          const overridden = !!draft.difficulties[id];
          const inherited = !overridden && !!effective.difficulties[id];
          const blocked = problems.some((p) => p.startsWith(`${id}:`) || p.startsWith(`${id},`));
          return (
            <button
              key={id}
              type="button"
              role="tab"
              id={`chain-tab-${id}`}
              aria-selected={level === id}
              aria-controls={`chain-panel-${id}`}
              className={blocked ? "chain-tab-blocked" : undefined}
              onClick={() => setLevel(id)}
            >
              {id}
              {blocked ? " · fix" : overridden ? " · edit" : inherited ? " · inherited" : ""}
            </button>
          );
        })}
      </nav>
      {DIFFICULTY_LEVELS.map((id) => {
        const own = draft.difficulties[id];
        const list = own ?? effective.difficulties[id];
        return (
          <div
            key={id}
            className="chain-level"
            role="tabpanel"
            id={`chain-panel-${id}`}
            aria-labelledby={`chain-tab-${id}`}
            hidden={level !== id}
          >
            <div className="chain-level-bar">
              <p className="small">
                Source: {setting?.difficulty_sources?.[id] ?? "legacy"} · revision{" "}
                {setting?.difficulty_revisions?.[id]?.slice(0, 12) ?? "—"}
                {!own && list ? " · inherited (read only until overridden)" : ""}
              </p>
              <div className="actions left">
                {!own && list && (
                  <button type="button" onClick={() => change(id, structuredClone(list))}>
                    Override {id}
                  </button>
                )}
                <button
                  type="button"
                  disabled={(own?.length ?? 0) >= 16 || !caps.length || (!own && !!list)}
                  onClick={() =>
                    change(id, [
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
                  Add {id} alternative
                </button>
                {own && (
                  <button type="button" onClick={() => change(id, undefined)}>
                    Inherit / reset {id}
                  </button>
                )}
              </div>
            </div>
            {!list && (
              <p className="muted small">
                {unconfigured ??
                  `No chain: built-in routing, harness from model preferences, executor order ${legacyExecutors?.[id]?.length ? legacyExecutors[id].join(" → ") : "none"}.`}{" "}
                Override or add an alternative to replace it.
              </p>
            )}
            {list?.map((alternative, index) => (
              <div className="chain-alt" key={`${id}-${index}`}>
                {!own ? (
                  <p className="small">
                    <strong>
                      {index + 1}. {alternative.id}
                    </strong>{" "}
                    · {alternative.mode === "single" ? "No executor" : "Harness + executor"} ·{" "}
                    {alternative.harness.provider} /{" "}
                    {alternative.harness.account_id ?? "any account"} · effort{" "}
                    {alternative.harness.effort ?? "runtime default"}
                    {alternative.mode === "paired"
                      ? ` → ${alternative.executor.provider} / ${alternative.executor.account_id ?? "any account"} · effort ${alternative.executor.effort ?? "runtime default"}`
                      : ""}
                  </p>
                ) : (
                  <>
                    <div className="chain-alt-head">
                      <span className="chain-alt-index">{index + 1}.</span>
                      <label>
                        ID
                        <input
                          aria-label={`${id} alternative ${index + 1} ID`}
                          aria-invalid={!ALT_ID.test(alternative.id)}
                          value={alternative.id}
                          onChange={(e) =>
                            change(
                              id,
                              own.map((a, i) => (i === index ? { ...a, id: e.target.value } : a)),
                            )
                          }
                        />
                      </label>
                      <label>
                        Mode
                        <select
                          aria-label={`${id} alternative ${index + 1} mode`}
                          value={alternative.mode}
                          onChange={(e) => {
                            const baseAlt = {
                              id: alternative.id,
                              runtime: alternative.runtime,
                              harness: alternative.harness,
                            };
                            change(
                              id,
                              own.map((a, i) =>
                                i !== index
                                  ? a
                                  : e.target.value === "single"
                                    ? { ...baseAlt, mode: "single" }
                                    : {
                                        ...baseAlt,
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
                      <span className="muted small">Runtime: {alternative.runtime}</span>
                      <div className="actions left">
                        <button
                          type="button"
                          disabled={index === 0}
                          onClick={() => {
                            const next = [...own];
                            [next[index - 1], next[index]] = [next[index]!, next[index - 1]!];
                            change(id, next);
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
                            change(id, next);
                          }}
                        >
                          Move down
                        </button>
                        <button
                          type="button"
                          onClick={() =>
                            change(
                              id,
                              own.filter((_, i) => i !== index),
                            )
                          }
                        >
                          Remove
                        </button>
                      </div>
                    </div>
                    <SelectorEditor
                      value={alternative.harness}
                      role="harness"
                      capabilities={caps}
                      accounts={accounts ?? []}
                      onChange={(harness) =>
                        change(
                          id,
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
                              id,
                              own.map((a, i) =>
                                i === index && a.mode === "paired" ? { ...a, executor } : a,
                              ),
                            )
                          }
                        />
                        <p className="muted small">
                          Local harnesses need agent or opencode. Same-provider native pairs share
                          one account; custom pairs need separate role capacity.
                        </p>
                      </>
                    )}
                  </>
                )}
              </div>
            ))}
          </div>
        );
      })}
    </section>
  );
}
