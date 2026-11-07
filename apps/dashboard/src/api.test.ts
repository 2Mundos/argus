// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  api,
  boardTasks,
  call,
  loadToken,
  newestTasks,
  saveToken,
  searchErrorMessage,
  setToken,
} from "./api";

const TOKEN_KEY = "argus.token";

afterEach(() => {
  vi.restoreAllMocks();
  setToken(null);
  sessionStorage.clear();
  localStorage.clear();
});

describe("token storage", () => {
  it("writes to sessionStorage and not localStorage", () => {
    saveToken("argus_admin_test");
    expect(sessionStorage.getItem(TOKEN_KEY)).toBe("argus_admin_test");
    expect(localStorage.getItem(TOKEN_KEY)).toBeNull();
  });

  it("migrates a leftover localStorage token once on read", () => {
    localStorage.setItem(TOKEN_KEY, "legacy-token");
    expect(loadToken()).toBe("legacy-token");
    expect(sessionStorage.getItem(TOKEN_KEY)).toBe("legacy-token");
    expect(localStorage.getItem(TOKEN_KEY)).toBeNull();
  });
});

describe("newestTasks", () => {
  it("reports each page as it arrives", async () => {
    const cursors: (string | undefined)[] = [];
    vi.spyOn(api, "GET").mockImplementation(((...args: unknown[]) => {
      const init = args[1] as { params?: { query?: { cursor?: string } } } | undefined;
      cursors.push(init?.params?.query?.cursor);
      const n = cursors.length;
      return Promise.resolve({
        data: {
          items: [{ id: String(n) }],
          total: 3,
          has_more: n < 3,
          next_cursor: n < 3 ? `c${n}` : null,
          omitted: [],
        },
        response: new Response(null, { status: 200 }),
      });
    }) as typeof api.GET);

    const seen: number[] = [];
    const result = await newestTasks(5, false, {
      onPage: (loaded) => seen.push(loaded.items.length),
    });
    expect(seen).toEqual([1, 2, 3]);
    expect(result.items.map((item) => item.id)).toEqual(["1", "2", "3"]);
    expect(result.total).toBe(3);
    expect(cursors[0]).toBeUndefined();
  });

  it("stops when the signal aborts", async () => {
    const controller = new AbortController();
    let n = 0;
    vi.spyOn(api, "GET").mockImplementation(((...args: unknown[]) => {
      const init = args[1] as { signal?: AbortSignal } | undefined;
      n += 1;
      if (n === 2) controller.abort();
      if (init?.signal?.aborted) {
        return Promise.reject(new DOMException("The operation was aborted.", "AbortError"));
      }
      return Promise.resolve({
        data: {
          items: [{ id: String(n) }],
          total: 9,
          has_more: true,
          next_cursor: "next",
          omitted: [],
        },
        response: new Response(null, { status: 200 }),
      });
    }) as typeof api.GET);

    const seen: number[] = [];
    await expect(
      newestTasks(5, false, {
        signal: controller.signal,
        onPage: (loaded) => seen.push(loaded.items.length),
      }),
    ).rejects.toMatchObject({ name: "AbortError" });
    expect(seen).toEqual([1]);
    expect(n).toBe(2);
  });
});

describe("boardTasks", () => {
  // One page per state group. The `held` group answers only once released.
  function serve(held: "active" | "ended") {
    const order: string[] = [];
    let release = () => {};
    const gate = new Promise<void>((resolve) => (release = resolve));
    vi.spyOn(api, "GET").mockImplementation((async (...args: unknown[]) => {
      const init = args[1] as { params: { query: { state: string[] } } };
      const group = init.params.query.state.includes("succeeded") ? "ended" : "active";
      order.push(group);
      if (group === held) await gate;
      const page = (id: string, total: number) => ({
        data: { items: [{ id }], total, has_more: false, next_cursor: null, omitted: [] },
        response: new Response(null, { status: 200 }),
      });
      return group === "active" ? page("a", 1) : page("e", 239);
    }) as typeof api.GET);
    return { order, release };
  }

  it("starts both chains at once and reports the merged board once", async () => {
    const { order, release } = serve("active");
    const seen: { ids: string[]; total: number; loading?: boolean }[] = [];
    const done = boardTasks(false, {
      onPage: (b) =>
        seen.push({ ids: b.items.map((t) => t.id), total: b.total, loading: b.loading }),
    });
    expect(order).toEqual(["active", "ended"]); // neither waits for the other
    release();
    const board = await done;
    // The ended page was ready first, but nothing painted before the active one.
    expect(seen).toEqual([{ ids: ["a", "e"], total: 240, loading: false }]);
    expect(board).toEqual({ items: [{ id: "a" }, { id: "e" }], total: 240, loading: false });
  });

  it("requests only the filtered states in one chain and reports its filtered total", async () => {
    const calls: string[][] = [];
    vi.spyOn(api, "GET").mockImplementation((async (...args: unknown[]) => {
      const init = args[1] as { params: { query: { state: string[] } } };
      calls.push(init.params.query.state);
      return {
        data: { items: [{ id: "b" }], total: 7, has_more: false, next_cursor: null, omitted: [] },
        response: new Response(null, { status: 200 }),
      };
    }) as typeof api.GET);
    const board = await boardTasks(false, { states: ["blocked"] });
    expect(calls).toEqual([["blocked"]]);
    expect(board).toEqual({ items: [{ id: "b" }], total: 7, loading: false });
  });

  it("reports the active tasks alone while the ended ones are loading", async () => {
    const { release } = serve("ended");
    let painted: (ids: string[]) => void = () => {};
    const first = new Promise<string[]>((resolve) => (painted = resolve));
    const done = boardTasks(false, {
      onPage: (b) => painted(b.items.map((t) => `${t.id}:${b.loading}`)),
    });
    expect(await first).toEqual(["a:true"]);
    release();
    await done;
  });
});

describe("task search", () => {
  const page = (next: string | null) =>
    Promise.resolve({
      data: { items: [], total: 0, has_more: next !== null, next_cursor: next, omitted: [] },
      response: new Response(null, { status: 200 }),
    });

  it("forwards q on every page of a cursor chain", async () => {
    const queries: unknown[] = [];
    vi.spyOn(api, "GET").mockImplementation(((...args: unknown[]) => {
      const init = args[1] as { params: { query: unknown } };
      queries.push(init.params.query);
      return page(queries.length < 2 ? "c1" : null);
    }) as typeof api.GET);
    await newestTasks(5, true, { q: "retry loop" });
    expect(queries).toMatchObject([
      { q: "retry loop", archived: true, cursor: undefined },
      { q: "retry loop", archived: true, cursor: "c1" },
    ]);
  });

  it("reads a searched board as one unfiltered chain", async () => {
    const queries: { state?: unknown; q?: string }[] = [];
    vi.spyOn(api, "GET").mockImplementation(((...args: unknown[]) => {
      queries.push((args[1] as { params: { query: never } }).params.query);
      return page(null);
    }) as typeof api.GET);
    expect(await boardTasks(false, { q: "x" })).toMatchObject({ items: [], loading: false });
    expect(queries).toHaveLength(1);
    expect(queries[0]).toMatchObject({ q: "x", state: undefined });
  });

  it("sends no q for the normal board", async () => {
    const queries: { q?: string }[] = [];
    vi.spyOn(api, "GET").mockImplementation(((...args: unknown[]) => {
      queries.push((args[1] as { params: { query: never } }).params.query);
      return page(null);
    }) as typeof api.GET);
    await boardTasks(false);
    expect(queries.every((query) => query.q === undefined)).toBe(true);
  });

  it("maps 422 search codes to validation and everything else to failure", async () => {
    const reject = async (status: number, detail: unknown): Promise<Error> => {
      try {
        await call(
          Promise.resolve({
            error: { detail },
            response: new Response(null, { status, statusText: "Bad" }),
          }),
        );
      } catch (err) {
        return err as Error;
      }
      throw new Error("call did not throw");
    };
    for (const code of ["query_empty", "query_too_broad"]) {
      const err = await reject(422, { code, message: "m" });
      expect(err).toBeInstanceOf(ApiError);
      expect(searchErrorMessage(err).kind).toBe("validation");
    }
    expect(
      searchErrorMessage(await reject(400, { code: "cursor_mismatch", message: "m" })).kind,
    ).toBe("failure");
    expect(searchErrorMessage(await reject(500, "boom")).text).toContain("boom");
    expect(searchErrorMessage(new TypeError("Failed to fetch")).kind).toBe("failure");
  });
});
