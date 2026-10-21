import { afterEach, describe, expect, it } from 'bun:test';
import fs from 'fs';
import os from 'os';
import path from 'path';

import {
  createClaudeUsageTracker,
  createCodexUsageTracker,
  readHarnessContext,
  recordHarnessUsage,
  USAGE_RELAY_FILE,
} from './harness-usage.js';

/** A Claude SDK result: `usage` is the turn, `modelUsage` and total_cost_usd the running total. */
const result = (
  turn: { i: number; o: number; cr?: number; cc?: number },
  running: { i: number; o: number; cr?: number; cc?: number },
  cost: number | null,
  uuid?: string,
) => ({
  type: 'result',
  uuid,
  usage: {
    input_tokens: turn.i,
    output_tokens: turn.o,
    cache_read_input_tokens: turn.cr ?? 0,
    cache_creation_input_tokens: turn.cc ?? 0,
  },
  modelUsage: {
    'claude-sonnet-5-5': {
      inputTokens: running.i,
      outputTokens: running.o,
      cacheReadInputTokens: running.cr ?? 0,
      cacheCreationInputTokens: running.cc ?? 0,
      costUSD: cost ?? 0,
    },
  },
  total_cost_usd: cost,
});

describe('Claude tracker: streaming input mode', () => {
  it('counts each turn once although modelUsage and the cost are running totals', () => {
    const t = createClaudeUsageTracker();
    const a = t.observe(result({ i: 10, o: 5, cr: 100, cc: 20 }, { i: 10, o: 5, cr: 100, cc: 20 }, 0.1));
    const b = t.observe(result({ i: 4, o: 3, cr: 150 }, { i: 14, o: 8, cr: 250, cc: 20 }, 0.15));
    const c = t.observe(result({ i: 1, o: 1 }, { i: 15, o: 9, cr: 250, cc: 20 }, 0.16));
    expect(a).toEqual({
      kind: 'measured',
      delta: { input: 30, output: 5, cached: 100, cost: null },
      model: 'claude-sonnet-5-5',
    });
    expect(b).toMatchObject({ kind: 'measured', delta: { input: 4, output: 3, cached: 150 } });
    expect(b.kind === 'measured' && b.delta.cost).toBeCloseTo(0.05, 6);
    expect(c).toMatchObject({ kind: 'measured', delta: { input: 1, output: 1, cached: 0 } });
    // The three deltas add up to the final running total: nothing doubled, nothing lost.
    const sum = [a, b, c].reduce(
      (s, o) => (o.kind === 'measured' ? { i: s.i + o.delta.input, o: s.o + o.delta.output, c: s.c + o.delta.cached } : s),
      { i: 0, o: 0, c: 0 },
    );
    expect(sum).toEqual({ i: 15 + 20, o: 9, c: 250 });
  });

  it('a resumed session restores earlier spend into the running total: the first turn is its own usage', () => {
    const t = createClaudeUsageTracker();
    // restored 1000/500 tokens and $2 from the session's history, plus this turn's 7/3
    const first = t.observe(result({ i: 7, o: 3 }, { i: 1007, o: 503 }, 2.05));
    expect(first).toMatchObject({ kind: 'measured', delta: { input: 7, output: 3, cached: 0, cost: null } });
    const second = t.observe(result({ i: 2, o: 2 }, { i: 1009, o: 505 }, 2.07));
    expect(second).toMatchObject({ kind: 'measured', delta: { input: 2, output: 2 } });
  });

  it('subagent tokens (only in modelUsage) are counted from the second turn on', () => {
    const t = createClaudeUsageTracker();
    t.observe(result({ i: 5, o: 5 }, { i: 5, o: 5 }, 0.01));
    const r = t.observe(result({ i: 5, o: 5 }, { i: 50, o: 25 }, 0.05));
    expect(r).toMatchObject({ kind: 'measured', delta: { input: 45, output: 20 } });
  });

  it('a total that shrinks (/clear, crash) is a reset: the turn counts by its own usage', () => {
    const t = createClaudeUsageTracker();
    t.observe(result({ i: 100, o: 100 }, { i: 100, o: 100 }, 1));
    const r = t.observe(result({ i: 3, o: 2 }, { i: 3, o: 2 }, 0.01));
    expect(r).toMatchObject({ kind: 'measured', delta: { input: 3, output: 2, cost: null } });
  });

  it('the same result replayed adds nothing; a turn that spent nothing adds nothing', () => {
    const t = createClaudeUsageTracker();
    expect(t.observe(result({ i: 1, o: 1 }, { i: 1, o: 1 }, 0.01, 'u1')).kind).toBe('measured');
    expect(t.observe(result({ i: 1, o: 1 }, { i: 1, o: 1 }, 0.01, 'u1')).kind).toBe('none');
    expect(t.observe(result({ i: 0, o: 0 }, { i: 1, o: 1 }, 0.01, 'u2')).kind).toBe('none');
  });

  it('no figures is not_reported with a reason, never zeros; a missing cost stays null', () => {
    const t = createClaudeUsageTracker();
    expect(t.observe({ type: 'result', result: 'hi' })).toEqual({
      kind: 'not_reported',
      reason: 'no_usage_in_result',
      model: null,
    });
    const r = createClaudeUsageTracker().observe({
      type: 'result',
      usage: { input_tokens: 2, output_tokens: 1 },
    });
    expect(r).toEqual({
      kind: 'measured',
      delta: { input: 2, output: 1, cached: 0, cost: null },
      model: null,
    });
    expect(t.observe(null).kind).toBe('none');
  });
});

const tu = (
  total: { i: number; c: number; o: number },
  last: { i: number; c: number; o: number },
) => ({
  total: { totalTokens: 0, inputTokens: total.i, cachedInputTokens: total.c, cacheWriteInputTokens: 0, outputTokens: total.o, reasoningOutputTokens: 0 },
  last: { totalTokens: 0, inputTokens: last.i, cachedInputTokens: last.c, cacheWriteInputTokens: 0, outputTokens: last.o, reasoningOutputTokens: 0 },
  modelContextWindow: null,
});

describe('Codex tracker', () => {
  it('a turn is the growth of the thread total; the cached part is split from input', () => {
    const t = createCodexUsageTracker();
    // resumed thread: 5000 earlier tokens; this turn's two model calls move it by 300 then 200
    t.update(tu({ i: 5300, c: 4000, o: 120 }, { i: 300, c: 200, o: 20 }));
    t.update(tu({ i: 5500, c: 4100, o: 170 }, { i: 200, c: 100, o: 50 }));
    expect(t.complete('gpt-x')).toEqual({
      kind: 'measured',
      delta: { input: 500 - 300, output: 70, cached: 300, cost: null },
      model: 'gpt-x',
    });
    t.update(tu({ i: 5600, c: 4100, o: 180 }, { i: 100, c: 0, o: 10 }));
    expect(t.complete(null)).toMatchObject({ kind: 'measured', delta: { input: 100, output: 10, cached: 0 } });
  });

  it('a turn without a usage update is not_reported, and a repeated total adds nothing', () => {
    const t = createCodexUsageTracker();
    expect(t.complete('m')).toEqual({ kind: 'not_reported', reason: 'no_token_usage_update', model: 'm' });
    t.update(tu({ i: 10, c: 0, o: 5 }, { i: 10, c: 0, o: 5 }));
    expect(t.complete('m').kind).toBe('measured');
    t.update(tu({ i: 10, c: 0, o: 5 }, { i: 0, c: 0, o: 0 }));
    expect(t.complete('m').kind).toBe('none');
  });

  it('ignores a malformed update', () => {
    const t = createCodexUsageTracker();
    t.update({ total: { inputTokens: 'x' } });
    t.update(undefined);
    expect(t.complete(null).kind).toBe('not_reported');
  });
});

describe('recordHarnessUsage', () => {
  const dirs: string[] = [];
  afterEach(() => {
    for (const d of dirs.splice(0)) fs.rmSync(d, { recursive: true, force: true });
  });
  const world = (context: Record<string, unknown> | null) => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'argus-harness-usage-'));
    dirs.push(dir);
    const policy = path.join(dir, 'argus-model-policy.json');
    if (context) fs.writeFileSync(policy, JSON.stringify({ policy: {}, revision: 'r', context }));
    return { dir, env: { ARGUS_MODEL_POLICY_FILE: policy } as NodeJS.ProcessEnv, file: path.join(dir, USAGE_RELAY_FILE) };
  };
  const ctx = { task_id: 't1', attempt: 2, engine_account_id: 'acc-1' };

  it('writes an engine-role record for the pinned task and account, ids only', () => {
    const w = world(ctx);
    const obs = { kind: 'measured', delta: { input: 3.4, output: 2, cached: 1, cost: null }, model: 'claude-sonnet-5-5' } as const;
    expect(recordHarnessUsage('claude', obs, 'q1', 1, w.env)).toBe(true);
    const rec = JSON.parse(fs.readFileSync(w.file, 'utf8'));
    expect(rec).toMatchObject({
      v: 1,
      kind: 'usage',
      source: 'harness',
      role: 'engine',
      task_id: 't1',
      attempt: 2,
      account_id: 'acc-1',
      payload: {
        task_id: 't1',
        model: 'claude-sonnet-5-5',
        engine: 'claude',
        role: 'engine',
        measurement: 'measured',
        input_tokens: 3,
        output_tokens: 2,
        cached_input_tokens: 1,
        cost_usd: null,
        reason: null,
        run_key: 'h1:t1:2:claude:q1:1',
      },
    });
    expect(fs.readFileSync(w.file, 'utf8')).not.toMatch(/bearer|secret|authorization/i);
  });

  it('records an unmeasured turn with its reason and null figures', () => {
    const w = world(ctx);
    recordHarnessUsage('codex', { kind: 'not_reported', reason: 'no_token_usage_update', model: null }, 'q', 3, w.env);
    const { payload } = JSON.parse(fs.readFileSync(w.file, 'utf8'));
    expect(payload).toMatchObject({
      measurement: 'not_reported',
      reason: 'no_token_usage_update',
      input_tokens: null,
      output_tokens: null,
      cached_input_tokens: null,
      cost_usd: null,
    });
  });

  it('writes nothing outside an Argus task (no pinned context) or for nothing spent', () => {
    const none = world(null);
    const obs = { kind: 'measured', delta: { input: 1, output: 1, cached: 0, cost: null }, model: null } as const;
    expect(recordHarnessUsage('claude', obs, 'q', 1, none.env)).toBe(false);
    expect(fs.existsSync(none.file)).toBe(false);
    const noAccount = world({ task_id: 't1', attempt: 1, engine_account_id: null });
    expect(readHarnessContext(noAccount.env)).toBeNull();
    const ok = world(ctx);
    expect(recordHarnessUsage('claude', { kind: 'none' }, 'q', 1, ok.env)).toBe(false);
    expect(fs.existsSync(ok.file)).toBe(false);
  });
});
