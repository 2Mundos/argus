// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Task } from "../api";
import { TaskEditForm } from "./TaskEditForm";

const net = vi.hoisted(() => {
  const net = {
    patches: [] as { expected_version: string; changes: Record<string, unknown> }[],
    reply: { status: 200, body: {} as unknown },
    latest: {} as unknown,
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
    let status = 200;
    let body: unknown = net.latest;
    if (input.method === "PATCH") {
      net.patches.push(await input.json());
      ({ status, body } = net.reply);
    }
    return new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;
  return net;
});

const ALL = [
  "title",
  "spec",
  "acceptance",
  "priority",
  "required_capabilities",
  "knowledge",
  "depends_on",
  "account_providers",
];
const task = {
  id: "t1",
  title: "Old title",
  spec: "old spec",
  acceptance: ["a"],
  priority: 1,
  required_capabilities: [],
  knowledge: [],
  depends_on: [],
  state: "ready",
  version: "v1",
  editable_fields: ALL,
} as unknown as Task;

afterEach(() => {
  cleanup();
  net.patches = [];
});

describe("TaskEditForm", () => {
  it("is prefilled and sends only the changed field with the version", async () => {
    const done = vi.fn();
    net.reply = { status: 200, body: { ...task, title: "New", version: "v2" } };
    render(<TaskEditForm task={task} onDone={done} />);
    const title = screen.getByLabelText("Title");
    expect((title as HTMLInputElement).value).toBe("Old title");
    await userEvent.clear(title);
    await userEvent.type(title, "New");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(done).toHaveBeenCalled());
    expect(net.patches).toEqual([{ expected_version: "v1", changes: { title: "New" } }]);
    expect(done.mock.calls[0]?.[0].version).toBe("v2");
  });

  it("pins the harness engine and model", async () => {
    const done = vi.fn();
    net.reply = { status: 200, body: { ...task, version: "v2" } };
    const editable = { ...task, editable_fields: [...ALL, "harness_model"] } as Task;
    render(<TaskEditForm task={editable} onDone={done} />);
    await userEvent.selectOptions(screen.getByLabelText("Harness engine"), "claude");
    await userEvent.type(screen.getByLabelText("Harness model"), "claude-opus-5-5");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(done).toHaveBeenCalled());
    expect(net.patches).toEqual([
      {
        expected_version: "v1",
        changes: { harness_model: { engine: "claude", model: "claude-opus-5-5" } },
      },
    ]);
  });

  it("cancels without a request", async () => {
    const done = vi.fn();
    render(<TaskEditForm task={task} onDone={done} />);
    await userEvent.type(screen.getByLabelText("Title"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(done).toHaveBeenCalledWith();
    expect(net.patches).toEqual([]);
  });

  it("disables fields the state does not allow", () => {
    const claimed = { ...task, state: "claimed", editable_fields: ["priority"] } as Task;
    render(<TaskEditForm task={claimed} onDone={() => {}} />);
    expect((screen.getByLabelText("Title") as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText("Priority") as HTMLInputElement).disabled).toBe(false);
  });

  it("shows a local error without a request and server errors beside their field", async () => {
    render(<TaskEditForm task={task} onDone={() => {}} />);
    await userEvent.clear(screen.getByLabelText("Title"));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Title is required.")).toBeTruthy();
    expect(net.patches).toEqual([]);

    net.reply = {
      status: 422,
      body: { detail: { code: "dependency_cycle", message: "cycle", details: ["t1", "t2", "t1"] } },
    };
    await userEvent.type(screen.getByLabelText("Title"), "ok");
    await userEvent.type(screen.getByLabelText("Depends on (task ids, one per line)"), "t2");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/dependency cycle: t1 → t2 → t1/)).toBeTruthy();
    expect((screen.getByLabelText("Title") as HTMLInputElement).value).toBe("ok");
  });

  it("keeps the draft on a conflict and re-bases it on the latest version", async () => {
    net.reply = {
      status: 409,
      body: { detail: { code: "stale_version", message: "stale", details: ["v9"] } },
    };
    net.latest = { ...task, priority: 4, version: "v9" };
    render(<TaskEditForm task={task} onDone={() => {}} />);
    await userEvent.clear(screen.getByLabelText("Title"));
    await userEvent.type(screen.getByLabelText("Title"), "Mine");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/now at version v9/)).toBeTruthy();
    expect((screen.getByLabelText("Title") as HTMLInputElement).value).toBe("Mine");

    net.reply = { status: 200, body: { ...task, title: "Mine", version: "v10" } };
    await userEvent.click(
      screen.getByRole("button", { name: "Keep editing on the latest version" }),
    );
    await waitFor(() => expect(screen.queryByText(/now at version/)).toBeNull());
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(net.patches.length).toBe(2));
    expect(net.patches[1]).toEqual({ expected_version: "v9", changes: { title: "Mine" } });
  });

  it("reload latest discards the draft", async () => {
    net.reply = {
      status: 409,
      body: { detail: { code: "stale_version", message: "stale", details: ["v9"] } },
    };
    net.latest = { ...task, title: "Theirs", version: "v9" };
    render(<TaskEditForm task={task} onDone={() => {}} />);
    await userEvent.type(screen.getByLabelText("Title"), "!");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await userEvent.click(await screen.findByRole("button", { name: "Reload latest" }));
    await waitFor(() =>
      expect((screen.getByLabelText("Title") as HTMLInputElement).value).toBe("Theirs"),
    );
  });
});
