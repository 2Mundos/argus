// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ArchiveTasks } from "./ArchiveTasks";

const net = vi.hoisted(() => {
  const net = { posts: [] as { dry_run: boolean; states: string[] }[], count: 3 };
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
    const path = new URL(input.url).pathname;
    let body: unknown = [{ slug: "web", name: "Web" }];
    if (path === "/api/tasks/archive") {
      const sent = await input.json();
      net.posts.push(sent);
      body = { count: net.count, dry_run: sent.dry_run };
    }
    return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
  return net;
});

vi.mock("../live", async () => {
  const { useEffect, useState } = await import("react");
  return {
    useLiveData: <T,>(load: () => Promise<T>) => {
      const [data, setData] = useState<T>();
      useEffect(() => {
        load().then(setData);
      }, []);
      return { data, error: undefined, reload: () => {} };
    },
  };
});

afterEach(() => {
  cleanup();
  net.posts = [];
});

describe("ArchiveTasks", () => {
  it("shows the dry-run count on the confirm button and archives only after the click", async () => {
    const onClose = vi.fn();
    render(<ArchiveTasks onClose={onClose} />);
    const confirm = await screen.findByRole("button", { name: "Archive 3 tasks" });
    expect(net.posts).toEqual([{ states: ["failed", "cancelled"], dry_run: true }]);
    expect(onClose).not.toHaveBeenCalled();

    await userEvent.click(confirm);
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(net.posts.at(-1)).toEqual({ states: ["failed", "cancelled"], dry_run: false });
  });

  it("recounts when the criteria change and cannot archive an empty selection", async () => {
    net.count = 0;
    render(<ArchiveTasks onClose={() => {}} />);
    const button = await screen.findByRole("button", { name: "Nothing to archive" });
    expect((button as HTMLButtonElement).disabled).toBe(true);

    net.count = 5;
    await userEvent.click(screen.getByLabelText("succeeded"));
    await screen.findByRole("button", { name: "Archive 5 tasks" });
    expect(net.posts.at(-1)).toEqual({
      states: ["succeeded", "failed", "cancelled"],
      dry_run: true,
    });
  });
});
