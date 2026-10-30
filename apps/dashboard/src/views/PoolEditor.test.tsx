// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Pool, TemplateCatalog } from "../api";
import { catalog, nanoclawCatalog } from "../catalogFixture";
import { PoolEditor, resetSessionCatalogForTests } from "./PoolEditor";

const net = vi.hoisted(() => {
  const net = { handler: (_: Request): Response | Promise<Response> => new Response("{}") };
  globalThis.fetch = (async (input: Request) => net.handler(input)) as typeof fetch;
  const Base = globalThis.Request;
  globalThis.Request = class extends Base {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      // openapi-fetch passes AbortSignal from React effects; jsdom rejects cross-realm signals.
      const next = init?.signal ? { ...init, signal: undefined } : init;
      super(
        typeof input === "string" && input.startsWith("/") ? `http://localhost${input}` : input,
        next,
      );
    }
  } as typeof Request;
  return net;
});

afterEach(() => {
  cleanup();
  resetSessionCatalogForTests();
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const pool = {
  name: "nc",
  target: "docker",
  runtime: "nanoclaw",
  image: "argus-nanoclaw-host:dev",
  capabilities: [],
  agents_per_instance: 1,
  desired_replicas: 1,
  max_replicas: 3,
  env: { TOKEN: "***" },
  cpus: 2,
  memory_mb: 512,
  allow_mutable_images: false,
  docker: {
    host: null,
    network: "br0",
    extra_hosts: ["h:host-gateway"],
    runtime: "sysbox-runc",
  },
  kubernetes: null,
  nanoclaw: {
    install_dir: "/i",
    instances_root: "/r",
    agent_image: "nc-agent:1",
    onecli_url: "http://host.docker.internal:10254",
    docker_socket: null,
    agent_image_archive: "/a.tar",
    repositories: {},
    user: null,
    group_add: [],
  },
  template: "nanoclaw-sandbox",
  status: "ok",
  last_error: null,
  live_instances: 0,
  version: "v1",
} as unknown as Pool;

/** Serve the catalog for GET /api/fleet/templates; hand every other request to `other`. */
function serve(
  cat: TemplateCatalog | Response,
  other: (req: Request) => Response | Promise<Response> = () => json(pool),
) {
  const seen: Request[] = [];
  net.handler = (input) => {
    seen.push(input.clone());
    if (input.url.includes("/api/fleet/templates")) {
      return cat instanceof Response ? cat.clone() : json(cat);
    }
    return other(input);
  };
  return seen;
}
const writes = (seen: Request[]) => seen.filter((r) => r.method !== "GET");
const open = (p: Pool | undefined = pool) => render(<PoolEditor pool={p} onClose={() => {}} />);
const ready = () => screen.findByRole("option", { name: "nc-agent:1" });
const save = () => userEvent.click(screen.getByRole("button", { name: "Save" }));

describe("PoolEditor images", () => {
  it("labels the host image and the NanoClaw agent image separately, from the pool", async () => {
    serve(nanoclawCatalog());
    open();
    await ready();
    const host = screen.getByLabelText("Host image") as HTMLSelectElement;
    const agent = screen.getByLabelText("NanoClaw agent image") as HTMLSelectElement;
    expect(host.value).toBe("argus-nanoclaw-host:dev");
    expect(agent.value).toBe("nc-agent:1");
    expect(screen.getByText(/Docker image of the runtime host.*Not the agent image/)).toBeTruthy();
    expect(screen.getByText(/agent containers the host starts.*Not the host image/)).toBeTruthy();
    // Each field lists only its own role's choices.
    expect(
      await within(host).findByRole("option", { name: "argus-nanoclaw-host:dev" }),
    ).toBeTruthy();
    expect(within(host).queryByRole("option", { name: "nc-agent:1" })).toBeNull();
    expect(within(agent).queryByRole("option", { name: "argus-nanoclaw-host:dev" })).toBeNull();
  });

  it("changing only the agent image patches nanoclaw whole, with the version, and nothing else", async () => {
    const seen = serve(nanoclawCatalog());
    open();
    await ready();
    const agent = screen.getByLabelText("NanoClaw agent image");
    await userEvent.selectOptions(agent, "Other: type a reference…");
    const typed = screen.getByLabelText("NanoClaw agent image (typed reference)");
    await userEvent.clear(typed);
    await userEvent.type(typed, "nc-agent:2");
    await save();
    await waitFor(() => expect(writes(seen).length).toBe(1));
    const [req] = writes(seen);
    expect(req?.method).toBe("PATCH");
    expect(await req?.json()).toEqual({
      expected_version: "v1",
      nanoclaw: { ...pool.nanoclaw, agent_image: "nc-agent:2" },
    });
  });

  it("keeps a stored value that the catalog does not list and sends nothing for it", async () => {
    const odd = { ...pool, nanoclaw: { ...pool.nanoclaw, agent_image: "legacy-agent:9" } } as Pool;
    const seen = serve(nanoclawCatalog());
    open(odd);
    await screen.findByRole("option", { name: /legacy-agent:9/ });
    const agent = (await screen.findByLabelText("NanoClaw agent image")) as HTMLSelectElement;
    expect(agent.value).toBe("legacy-agent:9");
    expect(
      within(agent).getByRole("option", { name: /legacy-agent:9 \(current, not in the catalog\)/ }),
    ).toBeTruthy();
    fireEvent.change(screen.getByLabelText(/Max replicas/), { target: { value: "5" } });
    await save();
    await waitFor(() => expect(writes(seen).length).toBe(1));
    const body = await writes(seen)[0]?.json();
    expect(body).toEqual({ expected_version: "v1", max_replicas: 5 });
  });

  it("shows an error state, keeps the value, and offers manual entry and retry", async () => {
    const seen = serve(json({ detail: "boom" }, 500));
    open();
    expect(await screen.findAllByText(/image catalog could not be loaded/)).toHaveLength(2);
    expect((screen.getByLabelText("Host image") as HTMLInputElement).value).toBe(
      "argus-nanoclaw-host:dev",
    );
    expect((screen.getByLabelText("NanoClaw agent image") as HTMLInputElement).value).toBe(
      "nc-agent:1",
    );
    const reads = seen.length;
    await userEvent.click(screen.getAllByRole("button", { name: "Retry loading the catalog" })[0]!);
    await waitFor(() => expect(seen.length).toBeGreaterThan(reads));
  });

  it("explains an empty catalog and keeps the stored value", async () => {
    const plain = {
      ...pool,
      runtime: "plain",
      nanoclaw: null,
      docker: { ...pool.docker, runtime: null },
    } as unknown as Pool;
    serve(catalog({ images: [] }));
    open(plain);
    expect(await screen.findByText(/No Argus worker image was found on this node/)).toBeTruthy();
    expect((screen.getByLabelText("Host image") as HTMLInputElement).value).toBe(
      "argus-nanoclaw-host:dev",
    );
    expect(screen.queryByLabelText("NanoClaw agent image")).toBeNull();
  });

  it("is manual-only on Kubernetes and says there is no verified list", async () => {
    const k8s = {
      ...pool,
      target: "kubernetes",
      runtime: "plain",
      nanoclaw: null,
      kubernetes: { context: "c", namespace: "default" },
      docker: { host: null, network: null, extra_hosts: [], runtime: null },
    } as unknown as Pool;
    serve(nanoclawCatalog());
    open(k8s);
    expect(await screen.findByText(/does not list images in a Kubernetes cluster/)).toBeTruthy();
    expect(screen.getByLabelText("Host image").tagName).toBe("INPUT");
  });

  it("refuses to save the NanoClaw agent image as the host image", async () => {
    const seen = serve(nanoclawCatalog());
    open();
    await screen.findByRole("option", { name: "argus-nanoclaw-host:dev" });
    await userEvent.selectOptions(screen.getByLabelText("Host image"), "Other: type a reference…");
    const typed = screen.getByLabelText("Host image (typed reference)");
    await userEvent.clear(typed);
    await userEvent.type(typed, "nc-agent:1");
    await save();
    expect(await screen.findByText(/is the NanoClaw agent image/)).toBeTruthy();
    expect(writes(seen)).toHaveLength(0);
  });

  it("still refuses a host/agent mix-up when the catalog is in error, using a previously loaded catalog", async () => {
    serve(nanoclawCatalog());
    open();
    await screen.findByRole("option", { name: "argus-nanoclaw-host:dev" });
    cleanup();
    // Session keeps the last successful catalog; a fresh mount that fails to load still uses it
    // for cross-role checks (and does not show the unverified-roles hint).
    const seen = serve(json({ detail: "boom" }, 500));
    open();
    expect(await screen.findAllByText(/image catalog could not be loaded/)).toHaveLength(2);
    expect(
      screen.queryByText(/role names cannot be verified against configured images/),
    ).toBeNull();
    const host = screen.getByLabelText("Host image") as HTMLInputElement;
    await userEvent.clear(host);
    await userEvent.type(host, "nc-agent:1");
    const agent = screen.getByLabelText("NanoClaw agent image") as HTMLInputElement;
    await userEvent.clear(agent);
    await userEvent.type(agent, "nc-agent:2");
    await save();
    expect(await screen.findByText(/is the NanoClaw agent image/)).toBeTruthy();
    expect(writes(seen)).toHaveLength(0);
  });

  it("rejects an invalid typed reference before sending", async () => {
    const seen = serve(nanoclawCatalog());
    open();
    await ready();
    await userEvent.selectOptions(
      screen.getByLabelText("NanoClaw agent image"),
      "Other: type a reference…",
    );
    const typed = screen.getByLabelText("NanoClaw agent image (typed reference)");
    await userEvent.clear(typed);
    await userEvent.type(typed, "not valid");
    await save();
    expect(await screen.findByText(/Not a valid image reference/)).toBeTruthy();
    expect(writes(seen)).toHaveLength(0);
  });
});

describe("PoolEditor env", () => {
  it("initialises env as *** and never sends it when unchanged or kept", async () => {
    const seen = serve(nanoclawCatalog());
    open();
    const env = screen.getByLabelText(/Environment/) as HTMLTextAreaElement;
    expect(env.value).toBe("TOKEN=***");
    fireEvent.change(env, { target: { value: "TOKEN=***\nNEW=1" } });
    await save();
    await waitFor(() => expect(writes(seen).length).toBe(1));
    const raw = await writes(seen)[0]?.text();
    expect(JSON.parse(raw ?? "")).toEqual({ expected_version: "v1", env: { NEW: "1" } });
    expect(raw).not.toContain("***");
  });

  it("does not save a *** value for a key that has no stored value", async () => {
    const seen = serve(nanoclawCatalog());
    open();
    fireEvent.change(screen.getByLabelText(/Environment/), {
      target: { value: "TOKEN=***\nOTHER=***" },
    });
    await save();
    expect(await screen.findByText(/OTHER has none/)).toBeTruthy();
    expect(writes(seen)).toHaveLength(0);
  });

  it("sends a pure no-op as no request", async () => {
    const seen = serve(nanoclawCatalog());
    const onClose = vi.fn();
    render(<PoolEditor pool={pool} onClose={onClose} />);
    await save();
    expect(onClose).toHaveBeenCalled();
    expect(writes(seen)).toHaveLength(0);
  });
});

describe("PoolEditor concurrency and server errors", () => {
  const stale = () =>
    json(
      {
        detail: {
          code: "stale_version",
          message: "pool changed",
          details: ["v2"],
        },
      },
      409,
    );

  it("on a stale version keeps the draft, explains it, and re-bases on request", async () => {
    let latest = { ...pool, version: "v2", max_replicas: 9, desired_replicas: 2 } as Pool;
    let patches = 0;
    const seen = serve(nanoclawCatalog(), (req) => {
      if (req.method === "PATCH") {
        patches += 1;
        return patches === 1 ? stale() : json({ ...latest, version: "v3" });
      }
      return json(latest);
    });
    open();
    fireEvent.change(screen.getByLabelText(/Max replicas/), { target: { value: "5" } });
    await save();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/changed while you were editing \(now at version v2\)/);
    expect(alert.textContent).toMatch(/draft is kept/);
    expect((screen.getByLabelText(/Max replicas/) as HTMLInputElement).value).toBe("5");

    await userEvent.click(
      screen.getByRole("button", { name: "Keep my edits on the latest version" }),
    );
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    // My edit wins; the field I did not touch takes the newer value.
    expect((screen.getByLabelText(/Max replicas/) as HTMLInputElement).value).toBe("5");
    expect((screen.getByLabelText(/Desired replicas/) as HTMLInputElement).value).toBe("2");
    latest = { ...latest, version: "v3" };
    await save();
    await waitFor(() => expect(writes(seen).length).toBe(2));
    expect(await writes(seen)[1]?.json()).toMatchObject({
      expected_version: "v2",
      max_replicas: 5,
    });
  });

  it("sends the version read when the editor opened", async () => {
    const seen = serve(nanoclawCatalog(), () => stale());
    open();
    fireEvent.change(screen.getByLabelText(/Max replicas/), { target: { value: "5" } });
    await save();
    await screen.findByRole("alert");
    expect(await writes(seen)[0]?.json()).toMatchObject({ expected_version: "v1" });
  });

  it("discard-and-reload replaces the draft with the latest pool", async () => {
    const latest = { ...pool, version: "v2", max_replicas: 9 } as Pool;
    serve(nanoclawCatalog(), (req) => (req.method === "PATCH" ? stale() : json(latest)));
    open();
    fireEvent.change(screen.getByLabelText(/Max replicas/), { target: { value: "5" } });
    await save();
    await userEvent.click(
      await screen.findByRole("button", { name: "Discard my edits and reload" }),
    );
    await waitFor(() =>
      expect((screen.getByLabelText(/Max replicas/) as HTMLInputElement).value).toBe("9"),
    );
  });

  it("after Discard, the agent-image control matches the restored catalog value, not a stale Other input", async () => {
    const latest = {
      ...pool,
      version: "v2",
      nanoclaw: { ...pool.nanoclaw, agent_image: "nc-agent:1" },
    } as Pool;
    serve(nanoclawCatalog(), (req) => (req.method === "PATCH" ? stale() : json(latest)));
    open();
    await ready();
    await userEvent.selectOptions(
      screen.getByLabelText("NanoClaw agent image"),
      "Other: type a reference…",
    );
    const typed = screen.getByLabelText("NanoClaw agent image (typed reference)");
    await userEvent.clear(typed);
    await userEvent.type(typed, "custom-agent:9");
    await save();
    await userEvent.click(
      await screen.findByRole("button", { name: "Discard my edits and reload" }),
    );
    await waitFor(() => {
      const agent = screen.getByLabelText("NanoClaw agent image") as HTMLSelectElement;
      expect(agent.tagName).toBe("SELECT");
      expect(agent.value).toBe("nc-agent:1");
    });
    expect(screen.queryByLabelText("NanoClaw agent image (typed reference)")).toBeNull();
  });

  it("after Keep, a genuinely custom typed agent image stays in the Other input", async () => {
    const latest = {
      ...pool,
      version: "v2",
      max_replicas: 9,
      nanoclaw: { ...pool.nanoclaw, agent_image: "nc-agent:1" },
    } as Pool;
    serve(nanoclawCatalog(), (req) => (req.method === "PATCH" ? stale() : json(latest)));
    open();
    await ready();
    await userEvent.selectOptions(
      screen.getByLabelText("NanoClaw agent image"),
      "Other: type a reference…",
    );
    const typed = screen.getByLabelText("NanoClaw agent image (typed reference)");
    await userEvent.clear(typed);
    await userEvent.type(typed, "custom-agent:9");
    await save();
    await userEvent.click(
      await screen.findByRole("button", { name: "Keep my edits on the latest version" }),
    );
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    expect(
      (screen.getByLabelText("NanoClaw agent image (typed reference)") as HTMLInputElement).value,
    ).toBe("custom-agent:9");
    expect((screen.getByLabelText("NanoClaw agent image") as HTMLSelectElement).value).toBe(
      "\u0000custom",
    );
  });

  it("shows API validation errors next to the field and keeps the draft", async () => {
    serve(nanoclawCatalog(), () =>
      json(
        {
          detail: [
            {
              loc: ["body", "nanoclaw", "agent_image"],
              msg: "String should have at least 1 character",
              type: "string_too_short",
            },
          ],
        },
        422,
      ),
    );
    open();
    await ready();
    await userEvent.selectOptions(
      screen.getByLabelText("NanoClaw agent image"),
      "Other: type a reference…",
    );
    const typed = screen.getByLabelText("NanoClaw agent image (typed reference)");
    await userEvent.clear(typed);
    await userEvent.type(typed, "nc-agent:3");
    await save();
    const message = await screen.findByText(/at least 1 character/);
    expect(message.id).toBe(
      `${(screen.getByLabelText("NanoClaw agent image (typed reference)") as HTMLElement).id.replace("-manual", "")}-error`,
    );
    expect(
      (screen.getByLabelText("NanoClaw agent image (typed reference)") as HTMLInputElement).value,
    ).toBe("nc-agent:3");
  });

  it("shows a policy refusal (403) as a form message and keeps the draft", async () => {
    serve(nanoclawCatalog(), () =>
      json({ detail: { code: "forbidden", message: "admin token required" } }, 403),
    );
    open();
    fireEvent.change(screen.getByLabelText(/Max replicas/), { target: { value: "5" } });
    await save();
    expect((await screen.findByRole("alert")).textContent).toMatch(/admin token required/);
    expect((screen.getByLabelText(/Max replicas/) as HTMLInputElement).value).toBe("5");
  });
});

describe("PoolEditor create", () => {
  it("creates a plain pool with a PUT of the full body and offers verified worker images", async () => {
    const seen = serve(catalog({ images: ["echo:1"] }), () => json({ name: "p1" }));
    render(<PoolEditor onClose={() => {}} />);
    await userEvent.type(screen.getByLabelText("Name"), "p1");
    await userEvent.selectOptions(await screen.findByLabelText("Host image"), "echo:1");
    expect(screen.queryByLabelText("NanoClaw agent image")).toBeNull();
    await save();
    await waitFor(() => expect(writes(seen).length).toBe(1));
    const [req] = writes(seen);
    expect(req?.method).toBe("PUT");
    expect(req?.url).toContain("/api/pools/p1");
    expect(await req?.json()).toMatchObject({
      target: "docker",
      image: "echo:1",
      env: {},
      desired_replicas: 0,
      max_replicas: 3,
    });
  });
});
