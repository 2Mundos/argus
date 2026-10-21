import { api, call, type ModelPolicy } from "./api";

export type { ModelPolicy };
export type OptionalScope = "trivial" | "hard_harness" | "hard_implementation";
export type Scope = "harness" | "implementation" | OptionalScope;
export type Chains = Record<string, string[]>;
/** Optional maps: a policy without them keeps its revision, so each is only sent when present. */
export type Draft = { harness: Chains; implementation: Chains } & Partial<
  Record<OptionalScope, Chains>
>;

export const SCOPES: Scope[] = ["harness", "implementation"];
export const OPTIONAL_SCOPES: OptionalScope[] = ["trivial", "hard_harness", "hard_implementation"];
/** The scopes a draft edits: an optional one only while the policy has it. */
export const scopesOf = (d: Draft): Scope[] => [...SCOPES, ...OPTIONAL_SCOPES.filter((s) => d[s])];
const optionals = (d: Partial<Record<OptionalScope, Chains | null>>, f: (c: Chains) => Chains) =>
  Object.fromEntries(
    OPTIONAL_SCOPES.flatMap((s) => {
      const c = d[s];
      return c ? [[s, f(c)]] : [];
    }),
  ) as Partial<Record<OptionalScope, Chains>>;
export const KNOWN_ENGINES = ["codex", "claude", "opencode", "agent"];
/** The `agent` executor is the Cursor CLI. */
export const engineLabel = (e: string) => (e === "agent" ? "agent (Cursor)" : e);
// Mirrors the API: a CLI model ID, not a claim that the provider serves it.
export const MODEL_ID = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/;

export const fetchModelPolicy = () => call(api.GET("/api/models"));
export const saveModelPolicy = (draft: Draft) =>
  call(
    api.PUT("/api/models", {
      body: {
        harness: draft.harness,
        implementation: draft.implementation,
        ...optionals(draft, (c) => c),
      },
    }),
  );
export const restoreModelDefaults = () => call(api.DELETE("/api/models"));

export function toDraft(p: Pick<ModelPolicy, "harness" | "implementation" | OptionalScope>): Draft {
  const copy = (c: Chains): Chains =>
    Object.fromEntries(Object.entries(c).map(([k, v]) => [k, [...v]]));
  return {
    harness: copy(p.harness),
    implementation: copy(p.implementation),
    ...optionals(p, copy),
  };
}

export function withChain(d: Draft, scope: Scope, engine: string, chain: string[] | null): Draft {
  const next = toDraft(d);
  const chains = next[scope] ?? {};
  if (chain === null) delete chains[engine];
  else chains[engine] = chain;
  next[scope] = chains;
  return next;
}

export function moved(chain: string[], from: number, to: number): string[] {
  if (to < 0 || to >= chain.length) return chain;
  const next = [...chain];
  next.splice(to, 0, ...next.splice(from, 1));
  return next;
}

/** Why `model` cannot be appended to `chain`, or null. */
export function addProblem(chain: string[], model: string): string | null {
  if (!MODEL_ID.test(model)) return "Model IDs use letters, digits and . _ : / - (max 200)";
  if (chain.includes(model)) return `${model} is already in this chain`;
  if (chain.length >= 10) return "A chain holds at most 10 models";
  return null;
}

export const sourceLabel = (s: ModelPolicy["source"]) =>
  s === "file" ? "file" : s === "env" ? "env" : "defaults";
