// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { parseTaskFilter } from "../taskRoute";
import { Tasks } from "./Tasks";

const net = vi.hoisted(() => {
  const net = {
    urls: [] as URL[],
    reply: (_url: URL): Response => new Response("{}"),
  };
  const Base = globalThis.Request;
  globalThis.Request = class extends Base {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      super(
        typeof input === "string" && input.startsWith("/") ? `http://localhost${input}` : input,
        init,
      );
    }
  } as typeof Request;
  globalThis.fetch = (async (input: Request) => {
    const url = new URL(input.url);
    net.urls.push(url);
    return net.reply(url);
  }) as typeof fetch;
  return net;
});

// No event stream in tests: reload when the deps change.
vi.mock("../live", async () => {
  const { useEffect, useState } = await import("react");
  return {
    reportPage: () => {},
    useLiveData: <T,>(load: (s: AbortSignal) => Promise<T>, deps: unknown[]) => {
      const [data, setData] = useState<T>();
      const [error, setError] = useState<Error>();
      useEffect(() => {
        setData(undefined);
        setError(undefined);
        load(new AbortController().signal).then(setData, setError);
      }, deps);
      return { data, error, reload: () => {} };
    },
  };
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const task = (extra: object = {}) => ({
  id: "t1",
  title: "Fix retry loop",
  project: "argus",
  state: "ready",
  kind: "generic",
  priority: 0,
  attempt: 0,
  ...extra,
});

beforeEach(() => {
  net.urls = [];
  window.location.hash = "#tasks";
});
afterEach(cleanup);

const qs = () =>
  net.urls.filter((u) => u.pathname === "/api/tasks").map((u) => u.searchParams.get("q"));

/** Stands in for App: the hash is the filter, and a hash change re-renders the board. */
function Routed() {
  const [filter, setFilter] = useState(() => parseTaskFilter(window.location.hash));
  useEffect(() => {
    const onHash = () => setFilter(parseTaskFilter(window.location.hash));
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  return <Tasks filter={filter} />;
}

describe("Tasks board search", () => {
  it("sends q to the server on submit and shows bounded snippets as text", async () => {
    net.reply = (url) =>
      json({
        items: url.searchParams.get("q")
          ? [
              task({
                matches: [
                  { field: "spec", snippet: "<img src=x onerror=alert(1)>", truncated: false },
                ],
              }),
            ]
          : [],
        total: url.searchParams.get("q") ? 1 : 0,
        has_more: false,
        next_cursor: null,
        omitted: [],
      });
    render(<Routed />);
    await userEvent.type(screen.getByRole("searchbox", { name: "Search tasks" }), "retry{Enter}");
    expect(await screen.findByText(/onerror=alert\(1\)/)).toBeTruthy();
    expect(document.querySelector("img")).toBeNull();
    expect(qs()).toContain("retry");
    expect(window.location.hash).toBe("#tasks?q=retry");
  });

  it("restores the normal board on Clear", async () => {
    window.location.hash = "#tasks?q=retry";
    net.reply = () =>
      json({ items: [task()], total: 1, has_more: false, next_cursor: null, omitted: [] });
    render(<Routed />);
    await screen.findByText(/Fix retry loop/);
    await userEvent.click(screen.getByRole("button", { name: "Clear" }));
    await waitFor(() => expect(qs().at(-1)).toBeNull());
    expect(window.location.hash).toBe("#tasks");
  });

  it("tells empty results, a rejected search and a failed read apart", async () => {
    window.location.hash = "#tasks?q=nothing";
    net.reply = () =>
      json({ items: [], total: 0, has_more: false, next_cursor: null, omitted: [] });
    render(<Routed />);
    expect(await screen.findByText(/No tasks match/)).toBeTruthy();

    net.reply = () => json({ detail: { code: "query_too_broad", message: "m" } }, 422);
    await userEvent.type(screen.getByRole("searchbox"), "x{Enter}");
    expect((await screen.findByRole("alert")).textContent).toMatch(/at most 8 words/);
    expect(screen.queryByText(/No tasks match/)).toBeNull();

    net.reply = () => json({ detail: "boom" }, 500);
    await userEvent.type(screen.getByRole("searchbox"), "y{Enter}");
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toMatch(/Could not load tasks: boom/),
    );
    expect(screen.queryByText(/No tasks match/)).toBeNull();
  });
});
