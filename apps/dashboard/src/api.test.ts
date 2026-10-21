// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, loadToken, newestTasks, saveToken, setToken } from "./api";

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
