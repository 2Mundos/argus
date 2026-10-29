// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { CreateWorkerRequest, Pool } from "../api";
import { catalog } from "../catalogFixture";
import { GuidedWorkerForm, NewWorker } from "./GuidedWorker";

// jsdom has a base URL but Node's Request does not: make "/api/…" absolute before
// the API client captures the constructor.
const net = vi.hoisted(() => {
  const net = { handler: (_: Request): Response | Promise<Response> => new Response("{}") };
  // The client captures `fetch` when it is created, so install the dispatcher first.
  globalThis.fetch = (async (input: Request) => net.handler(input)) as typeof fetch;
  const Base = globalThis.Request;
  globalThis.Request = class extends Base {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      super(
        typeof input === "string" && input.startsWith("/") ? `http://localhost${input}` : input,
        init,
      );
    }
  } as typeof Request;
  return net;
});

afterEach(() => {
  cleanup();
});

describe("guided form", () => {
  it("preselects the ready template, local node, name and quantity 1, advanced closed", async () => {
    const onCreate = vi.fn(async (_: CreateWorkerRequest) => {});
    render(<GuidedWorkerForm catalog={catalog()} onCreate={onCreate} />);
    expect((screen.getByLabelText(/Argus API worker/) as HTMLInputElement).checked).toBe(true);
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("worker");
    expect((screen.getByLabelText("How many") as HTMLInputElement).value).toBe("1");
    expect(document.querySelector("details")?.open).toBe(false);
    expect(onCreate).not.toHaveBeenCalled(); // selecting creates nothing
    await userEvent.click(screen.getByRole("button", { name: "Create 1 worker" }));
    expect(onCreate).toHaveBeenCalledOnce();
    expect(onCreate.mock.calls[0]?.[0]).toEqual({
      template: "argus-worker",
      node: "local",
      name: "worker",
      quantity: 1,
      image: "echo:1",
    });
  });

  it("with nothing ready, nothing is chosen; a blocked choice can be inspected but not created", async () => {
    const onCreate = vi.fn(async () => {});
    render(<GuidedWorkerForm catalog={catalog({ worker: false })} onCreate={onCreate} />);
    expect(screen.getAllByRole("radio").every((r) => !(r as HTMLInputElement).checked)).toBe(true);
    expect(screen.getByText("Choose a worker type to continue.")).toBeTruthy();
    const button = screen.getByRole("button", { name: /Create/ }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    await userEvent.click(screen.getByLabelText(/NanoClaw, sandboxed/));
    expect(screen.getByRole("status").textContent).toMatch(/Not ready:.*sysbox-runc/);
    expect((screen.getByRole("button", { name: /Create/ }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    fireEvent.submit(button.closest("form") as HTMLFormElement);
    expect(onCreate).not.toHaveBeenCalled();
  });

  it("switching template shows its missing items and re-suggests an unedited name", async () => {
    const onCreate = vi.fn(async () => {});
    render(<GuidedWorkerForm catalog={catalog()} onCreate={onCreate} />);
    await userEvent.click(screen.getByLabelText(/NanoClaw, sandboxed/));
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("nanoclaw-sandbox");
    expect(screen.getByText(/Install the sysbox-runc runtime/)).toBeTruthy();
    expect((screen.getByRole("button", { name: /Create/ }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    await userEvent.click(screen.getByLabelText(/Argus API worker/));
    await userEvent.clear(screen.getByLabelText("Name"));
    await userEvent.type(screen.getByLabelText("Name"), "mine");
    await userEvent.click(screen.getByLabelText(/NanoClaw, sandboxed/));
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("mine");
  });

  it("sends advanced fields only when they were edited", async () => {
    const onCreate = vi.fn(async (_: CreateWorkerRequest) => {});
    render(<GuidedWorkerForm catalog={catalog()} onCreate={onCreate} />);
    await userEvent.click(screen.getByText("Advanced"));
    expect((screen.getByLabelText(/Capabilities/) as HTMLInputElement).value).toBe("python");
    await userEvent.clear(screen.getByLabelText(/Capabilities/));
    await userEvent.type(screen.getByLabelText(/Capabilities/), "gpu, git");
    fireEvent.change(screen.getByLabelText(/Environment/), { target: { value: "A=b" } });
    await userEvent.click(screen.getByRole("button", { name: "Create 1 worker" }));
    expect(onCreate.mock.calls[0]?.[0].advanced).toEqual({
      capabilities: ["gpu", "git"],
      env: { A: "b" },
    });
  });

  it("shows the default cap under Advanced and sends no cap unless it was changed", async () => {
    const onCreate = vi.fn(async (_: CreateWorkerRequest) => {});
    render(<GuidedWorkerForm catalog={catalog()} onCreate={onCreate} />);
    await userEvent.click(screen.getByText("Advanced"));
    const cap = screen.getByLabelText(/Max replicas/) as HTMLInputElement;
    expect(cap.value).toBe("3");
    fireEvent.change(screen.getByLabelText("How many"), { target: { value: "5" } });
    expect(cap.value).toBe("5"); // the server default follows the quantity
    await userEvent.click(screen.getByRole("button", { name: "Create 5 workers" }));
    expect(onCreate.mock.calls[0]?.[0].advanced).toBeUndefined();
  });

  it("sends an explicit cap and blocks one below the quantity", async () => {
    const onCreate = vi.fn(async (_: CreateWorkerRequest) => {});
    render(<GuidedWorkerForm catalog={catalog()} onCreate={onCreate} />);
    await userEvent.click(screen.getByText("Advanced"));
    fireEvent.change(screen.getByLabelText("How many"), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText(/Max replicas/), { target: { value: "1" } });
    const button = screen.getByRole("button", { name: /Create 2 workers/ }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(screen.getByText(/Cap must be/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText(/Max replicas/), { target: { value: "8" } });
    await userEvent.click(button);
    expect(onCreate.mock.calls[0]?.[0].advanced).toEqual({ max_replicas: 8 });
    expect(onCreate.mock.calls[0]?.[0].quantity).toBe(2);
  });

  it("requires choosing among several images and never sends an unlisted one", async () => {
    const onCreate = vi.fn(async (_: CreateWorkerRequest) => {});
    render(<GuidedWorkerForm catalog={catalog({ images: ["a:1", "b:1"] })} onCreate={onCreate} />);
    expect((screen.getByRole("button", { name: /Create/ }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    await userEvent.selectOptions(screen.getByLabelText("Image"), "b:1");
    await userEvent.click(screen.getByRole("button", { name: /Create/ }));
    expect(onCreate.mock.calls[0]?.[0].image).toBe("b:1");
  });

  it("a double click submits once and an error is shown, not hidden", async () => {
    let reject: (e: Error) => void = () => {};
    const onCreate = vi.fn(
      () =>
        new Promise<void>((_, r) => {
          reject = r;
        }),
    );
    render(<GuidedWorkerForm catalog={catalog()} onCreate={onCreate} />);
    const button = screen.getByRole("button", { name: /Create/ });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(onCreate).toHaveBeenCalledTimes(1);
    reject(new Error("409 pool_exists"));
    await waitFor(() => expect(screen.getByText("409 pool_exists")).toBeTruthy());
  });
});

describe("the two NanoClaw choices", () => {
  it("lists both, with the socket's warning, and never preselects the warned one", async () => {
    const onCreate = vi.fn(async (_: CreateWorkerRequest) => {});
    render(
      <GuidedWorkerForm catalog={catalog({ worker: false, socket: true })} onCreate={onCreate} />,
    );
    expect(screen.getByLabelText(/NanoClaw, sandboxed/)).toBeTruthy();
    expect(screen.getByLabelText(/NanoClaw, host Docker socket/)).toBeTruthy();
    expect(
      (screen.getByLabelText(/NanoClaw, host Docker socket/) as HTMLInputElement).checked,
    ).toBe(false);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows the warning before submit and needs it accepted; sandbox stays blocked", async () => {
    const onCreate = vi.fn(async (_: CreateWorkerRequest) => {});
    render(
      <GuidedWorkerForm catalog={catalog({ worker: false, socket: true })} onCreate={onCreate} />,
    );
    await userEvent.click(screen.getByLabelText(/NanoClaw, host Docker socket/));
    expect(screen.getByRole("alert").textContent).toMatch(/root-equivalent/);
    const create = screen.getByRole("button", { name: /Create 1 worker/ }) as HTMLButtonElement;
    expect(create.disabled).toBe(true);
    await userEvent.click(screen.getByLabelText(/I understand and accept/));
    expect(create.disabled).toBe(false);
    await userEvent.click(create);
    expect(onCreate.mock.calls[0]?.[0]).toMatchObject({
      template: "nanoclaw-socket",
      name: "nanoclaw-socket",
      quantity: 1,
    });
    // The sandboxed choice is a separate template and is not ready: no fallback.
    await userEvent.click(screen.getByLabelText(/NanoClaw, sandboxed/));
    expect(screen.getByText(/Install the sysbox-runc runtime/)).toBeTruthy();
    expect(
      (screen.getByRole("button", { name: /Create 1 worker/ }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("asks again after switching away and back", async () => {
    render(
      <GuidedWorkerForm
        catalog={catalog({ worker: true, socket: true })}
        onCreate={async () => {}}
      />,
    );
    await userEvent.click(screen.getByLabelText(/NanoClaw, host Docker socket/));
    await userEvent.click(screen.getByLabelText(/I understand and accept/));
    await userEvent.click(screen.getByLabelText(/Argus API worker/));
    await userEvent.click(screen.getByLabelText(/NanoClaw, host Docker socket/));
    expect((screen.getByLabelText(/I understand and accept/) as HTMLInputElement).checked).toBe(
      false,
    );
  });
});

function stubFetch(handler: (req: Request) => Response | Promise<Response>) {
  const seen: Request[] = [];
  net.handler = (input) => {
    seen.push(input.clone());
    return handler(input);
  };
  return seen;
}
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("NewWorker", () => {
  it("creates from the catalog and reports a request, not a running worker", async () => {
    const seen = stubFetch(async (req) =>
      req.method === "GET"
        ? json(catalog())
        : json({ pool: { name: "worker" }, requested: 1, desired_replicas: 1 }, 201),
    );
    render(<NewWorker />);
    await userEvent.click(await screen.findByRole("button", { name: "Create 1 worker" }));
    expect(await screen.findByText(/Requested 1 worker in pool worker/)).toBeTruthy();
    expect(screen.getByText(/not running yet/)).toBeTruthy();
    const post = seen.find((r) => r.method === "POST");
    expect(post?.url).toContain("/api/fleet/workers");
    expect(await post?.json()).toMatchObject({ template: "argus-worker", quantity: 1 });
  });

  it("shows a server refusal and no success", async () => {
    stubFetch(async (req) =>
      req.method === "GET"
        ? json(catalog())
        : json({ detail: { code: "pool_exists", message: "pool 'worker' already exists" } }, 409),
    );
    render(<NewWorker />);
    await userEvent.click(await screen.findByRole("button", { name: "Create 1 worker" }));
    expect(await screen.findByText(/already exists/)).toBeTruthy();
    expect(screen.queryByText(/Requested/)).toBeNull();
  });
});
