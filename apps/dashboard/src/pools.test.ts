import { describe, expect, it } from "vitest";
import { ApiError, type Pool } from "./api";
import { catalog, nanoclawCatalog } from "./catalogFixture";
import {
  buildPool,
  envPatch,
  imageChoices,
  imageRefError,
  parseEnv,
  poolErrors,
  toForm,
} from "./pools";

describe("envPatch", () => {
  const stored = { TOKEN: "***", MODE: "***" };

  it("sends nothing when every value is left redacted", () => {
    expect(envPatch(stored, { TOKEN: "***", MODE: "***" })).toBeUndefined();
  });

  it("sets changed and new values and removes dropped keys", () => {
    expect(envPatch(stored, { MODE: "slow", NEW: "1" })).toEqual({
      MODE: "slow",
      NEW: "1",
      TOKEN: null,
    });
  });

  it("never sends a literal ***, not even for a key that was not stored", () => {
    expect(envPatch({}, { MASK: "***" })).toBeUndefined();
    expect(JSON.stringify(envPatch(stored, { TOKEN: "***", NEW: "1" }))).not.toContain("***");
  });
});

describe("parseEnv", () => {
  it("accepts KEY=value lines, blank lines and values containing =", () => {
    expect(parseEnv("A=1\n\n B=x=y \nC=", {}).env).toEqual({ A: "1", B: "x=y", C: "" });
  });

  it("refuses instead of silently dropping a line", () => {
    expect(parseEnv("A=1\nnot a pair", {}).error).toMatch(/Line 2/);
    expect(parseEnv("1BAD=x", {}).error).toMatch(/Line 1/);
    expect(parseEnv("A=1\nA=2", {}).error).toMatch(/set twice/);
  });

  it("allows *** only for a stored key", () => {
    expect(parseEnv("TOKEN=***", { TOKEN: "***" }).error).toBeUndefined();
    expect(parseEnv("NEW=***", { TOKEN: "***" }).error).toMatch(/has none/);
  });
});

describe("imageRefError", () => {
  it.each([
    "argus-nanoclaw-host:dev",
    "ghcr.io/org/app:1.2",
    "localhost:5000/team/app",
    `repo/app@sha256:${"a".repeat(64)}`,
  ])("accepts %s", (ref) => expect(imageRefError(ref)).toBeUndefined());

  it.each(["", "  ", "Has Space:1", "UPPER:1", "bad ref", "-x", "a//b", "x".repeat(501)])(
    "rejects %j",
    (ref) => expect(imageRefError(ref)).toBeTruthy(),
  );
});

const nanoclawPool = {
  name: "nc",
  target: "docker",
  runtime: "nanoclaw",
  image: "host:dev",
  capabilities: ["git"],
  agents_per_instance: 1,
  desired_replicas: 1,
  max_replicas: 3,
  env: { TOKEN: "***" },
  cpus: null,
  memory_mb: null,
  allow_mutable_images: false,
  docker: { host: null, network: "br0", extra_hosts: ["h:host-gateway"], runtime: "sysbox-runc" },
  kubernetes: null,
  nanoclaw: {
    install_dir: "/opt/nc",
    instances_root: "/srv/i",
    agent_image: "agent:1",
    onecli_url: "http://host.docker.internal:10254",
    docker_socket: null,
    agent_image_archive: "/srv/a.tar",
    repositories: { p: "https://example.com/r.git" },
    user: null,
    group_add: [],
  },
  template: "nanoclaw-sandbox",
  version: "v1",
} as unknown as Pool;

describe("buildPool (edit)", () => {
  it("sends nothing for an untouched form", () => {
    const built = buildPool(nanoclawPool, toForm(nanoclawPool));
    expect(built.unchanged).toBe(true);
    expect(built.errors).toEqual({});
  });

  it("changing the agent image keeps every other nanoclaw option and leaves host image alone", () => {
    const form = { ...toForm(nanoclawPool), agentImage: "agent:2" };
    const { body } = buildPool(nanoclawPool, form);
    expect(body).toEqual({ nanoclaw: { ...nanoclawPool.nanoclaw, agent_image: "agent:2" } });
    expect(body).not.toHaveProperty("image");
  });

  it("changing the host image sends only image, not nanoclaw", () => {
    const { body } = buildPool(nanoclawPool, { ...toForm(nanoclawPool), image: "host:2" });
    expect(body).toEqual({ image: "host:2" });
  });

  it("keeps docker.runtime and network when another docker field changes", () => {
    const { body } = buildPool(nanoclawPool, { ...toForm(nanoclawPool), extraHosts: "a:b" });
    expect(body).toEqual({
      docker: { host: null, network: "br0", extra_hosts: ["a:b"], runtime: "sysbox-runc" },
    });
  });

  it("never round-trips redacted env: untouched or *** values are not in the body", () => {
    const form = { ...toForm(nanoclawPool), env: "TOKEN=***\nNEW=1", max: "5" };
    const { body } = buildPool(nanoclawPool, form);
    expect(body).toEqual({ max_replicas: 5, env: { NEW: "1" } });
    expect(JSON.stringify(body)).not.toContain("***");
  });

  it("refuses *** for a key with no stored value", () => {
    const form = { ...toForm(nanoclawPool), env: "TOKEN=***\nOTHER=***" };
    const built = buildPool(nanoclawPool, form);
    expect(built.body).toBeUndefined();
    expect(built.errors.env).toMatch(/OTHER/);
  });

  it("keeps a stored image that is not in the catalog, and does not validate it", () => {
    const odd = { ...nanoclawPool, image: "Weird Image" } as Pool;
    const built = buildPool(odd, { ...toForm(odd), max: "4" }, nanoclawCatalog());
    expect(built.errors).toEqual({});
    expect(built.body).toEqual({ max_replicas: 4 });
  });

  it("validates a changed manual image", () => {
    const built = buildPool(nanoclawPool, { ...toForm(nanoclawPool), image: "bad image" });
    expect(built.errors.image).toBeTruthy();
    expect(built.body).toBeUndefined();
  });

  it("refuses the agent image in the host field and the host image in the agent field", () => {
    const cat = nanoclawCatalog();
    const swapped = buildPool(nanoclawPool, { ...toForm(nanoclawPool), image: "nc-agent:1" }, cat);
    expect(swapped.errors.image).toMatch(/agent image/);
    const other = buildPool(
      nanoclawPool,
      { ...toForm(nanoclawPool), agentImage: "argus-nanoclaw-host:dev" },
      cat,
    );
    expect(other.errors.agentImage).toMatch(/host image/);
    const same = buildPool(nanoclawPool, { ...toForm(nanoclawPool), agentImage: "host:dev" });
    expect(same.errors.agentImage).toMatch(/different images/);
  });

  it("uses a last-known catalog for cross-role checks when the live catalog is absent", () => {
    const lastKnown = nanoclawCatalog();
    // Without any catalog, a distinct agent-image reference as host is not caught (only equality).
    const blind = buildPool(nanoclawPool, {
      ...toForm(nanoclawPool),
      image: "nc-agent:1",
      agentImage: "nc-agent:2",
    });
    expect(blind.errors.image).toBeUndefined();
    expect(blind.body).toMatchObject({ image: "nc-agent:1" });
    // The editor passes the last successfully loaded catalog so the mix-up is still refused.
    const withLast = buildPool(
      nanoclawPool,
      { ...toForm(nanoclawPool), image: "nc-agent:1", agentImage: "nc-agent:2" },
      lastKnown,
    );
    expect(withLast.body).toBeUndefined();
    expect(withLast.errors.image).toMatch(/agent image/);
  });

  it("checks numbers", () => {
    const built = buildPool(nanoclawPool, { ...toForm(nanoclawPool), desired: "9", max: "3" });
    expect(built.errors.desired).toMatch(/cannot exceed/);
    expect(
      buildPool(nanoclawPool, { ...toForm(nanoclawPool), cpus: "0" }).errors.cpus,
    ).toBeTruthy();
  });
});

describe("buildPool (create)", () => {
  it("builds a plain pool with the full body and no redacted values", () => {
    const form = { ...toForm(), name: "p1", image: "echo:1", env: "A=1" };
    const { body, errors } = buildPool(undefined, form);
    expect(errors).toEqual({});
    expect(body).toMatchObject({
      target: "docker",
      image: "echo:1",
      env: { A: "1" },
      desired_replicas: 0,
      max_replicas: 3,
      docker: { host: null, network: null, extra_hosts: ["host.docker.internal:host-gateway"] },
    });
    expect(body).not.toHaveProperty("nanoclaw");
  });

  it("refuses a missing image, a bad name and *** values", () => {
    const { errors } = buildPool(undefined, { ...toForm(), name: "Bad Name", env: "A=***" });
    expect(Object.keys(errors).sort()).toEqual(["env", "image", "name"]);
  });
});

describe("imageChoices", () => {
  const where = { runtime: "nanoclaw", target: "docker", dockerHost: "" } as const;

  it("keeps host and agent roles apart", () => {
    const cat = nanoclawCatalog();
    const host = imageChoices(cat, "host", where);
    const agent = imageChoices(cat, "agent", where);
    expect(host.kind === "options" && host.options.map((o) => o.value)).toEqual([
      "argus-nanoclaw-host:dev",
    ]);
    expect(agent.kind === "options" && agent.options.map((o) => o.value)).toEqual(["nc-agent:1"]);
  });

  it("offers verified Argus worker images for a plain docker pool, and no agent image", () => {
    const cat = catalog({ images: ["echo:1", "py:2"] });
    const plain = { runtime: "plain", target: "docker", dockerHost: "" } as const;
    const host = imageChoices(cat, "host", plain);
    expect(host.kind === "options" && host.options.map((o) => o.value)).toEqual(["echo:1", "py:2"]);
    expect(imageChoices(cat, "agent", plain).kind).toBe("manual-only");
  });

  it("is empty when the catalog has nothing for the node", () => {
    const cat = catalog({ images: [] });
    const r = imageChoices(cat, "host", { runtime: "plain", target: "docker", dockerHost: "" });
    expect(r.kind).toBe("empty");
  });

  it("is manual only on kubernetes and for an unknown Docker host", () => {
    const cat = nanoclawCatalog();
    expect(imageChoices(cat, "host", { ...where, target: "kubernetes" }).kind).toBe("manual-only");
    expect(imageChoices(cat, "host", { ...where, dockerHost: "ssh://x@y" }).kind).toBe(
      "manual-only",
    );
  });

  it("says so when the node is unreachable", () => {
    const cat = nanoclawCatalog();
    const [node] = cat.nodes;
    if (node) Object.assign(node, { available: false, error: "daemon down" });
    const r = imageChoices(cat, "host", where);
    expect(r.kind === "empty" && r.reason).toMatch(/daemon down/);
  });
});

describe("poolErrors", () => {
  it("maps API field paths onto form fields", () => {
    const err = new ApiError(422, "validation_error", "rejected", [
      "nanoclaw.agent_image: String should have at least 1 character",
      "docker.extra_hosts.0: bad",
      "env.TOKEN: bad",
    ]);
    expect(poolErrors(err)).toEqual({
      agentImage: "String should have at least 1 character",
      extraHosts: "bad",
      env: "bad",
    });
  });

  it("falls back to a form-level message", () => {
    const err = new ApiError(403, "docker_socket_not_allowed", "needs opt-in");
    expect(poolErrors(err)).toEqual({ form: "needs opt-in" });
  });
});
