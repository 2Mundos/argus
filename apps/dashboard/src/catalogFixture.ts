import type { TemplateCatalog } from "./api";

export const GATEWAY_NOTE =
  "Ready means the host can start and register with Argus. Task execution also requires a running OneCLI gateway and configured model credentials; these are not verified by this catalog.";

const check = (name: string, ok: boolean | null, detail = name) => ({ name, ok, detail });
const defaults = (name: string) => ({
  name,
  quantity: 1,
  max_replicas: 3,
  capabilities: [],
  extra_hosts: ["host.docker.internal:host-gateway"],
});

export function catalog(
  over: { worker?: boolean; nanoclaw?: boolean; socket?: boolean; images?: string[] } = {},
): TemplateCatalog {
  const images = (over.images ?? ["echo:1"]).map((image) => ({
    image,
    capabilities: ["python"],
    source: "label",
  }));
  const worker = over.worker ?? true;
  const nc = over.nanoclaw ?? false;
  const sock = over.socket ?? false;
  return {
    public_url_configured: true,
    default_node: "local",
    pools: 0,
    nodes: [
      {
        id: "local",
        label: "This machine (local Docker)",
        default: true,
        available: true,
        error: null,
        version: "27",
        runtimes: ["runc"],
        cpus: 4,
        memory_mb: 8192,
        shares_filesystem: true,
      },
    ],
    templates: [
      {
        id: "nanoclaw-sandbox",
        name: "NanoClaw, sandboxed",
        description: "Sandboxed NanoClaw host",
        runtime: "nanoclaw",
        notes: ["Isolated.", GATEWAY_NOTE],
        warning: null,
        setup: ["Install the sandbox runtime"],
        defaults: defaults("nanoclaw-sandbox"),
        ready: nc,
        readiness: [
          {
            node: "local",
            ready: nc,
            checks: [
              check(
                "oci_runtime",
                nc,
                "sandbox runtime sysbox-runc not offered (the daemon has ['runc'])",
              ),
            ],
            missing: nc ? [] : ["Install the sysbox-runc runtime on this Docker host"],
            images: [],
          },
        ],
      },
      {
        id: "nanoclaw-socket",
        name: "NanoClaw, host Docker socket",
        description: "NanoClaw host with the Docker socket",
        runtime: "nanoclaw",
        notes: [GATEWAY_NOTE],
        warning: "Not sandboxed: root-equivalent on this Docker host.",
        setup: ["Opt in"],
        defaults: defaults("nanoclaw-socket"),
        ready: sock,
        readiness: [
          {
            node: "local",
            ready: sock,
            checks: [
              check("operator_opt_in", sock, "the operator has not allowed the Docker socket"),
            ],
            missing: sock ? [] : ["Set ARGUS_NANOCLAW_ALLOW_DOCKER_SOCKET=true"],
            images: [],
          },
        ],
      },
      {
        id: "argus-worker",
        name: "Argus API worker",
        description: "An image that speaks the Argus API",
        runtime: "plain",
        notes: [],
        warning: null,
        setup: ["Label an image"],
        defaults: defaults("worker"),
        ready: worker,
        readiness: [
          {
            node: "local",
            ready: worker,
            checks: [check("x", worker)],
            missing: worker ? [] : ["Label an installed image argus.worker=plain"],
            images: worker ? images : [],
          },
        ],
      },
    ],
  } as TemplateCatalog;
}

/** The catalog with both NanoClaw templates reporting their configured host and agent images. */
export function nanoclawCatalog(): TemplateCatalog {
  const cat = catalog({ nanoclaw: true, socket: true });
  for (const t of cat.templates) {
    if (t.runtime !== "nanoclaw") continue;
    for (const r of t.readiness) {
      r.host_image = {
        image: "argus-nanoclaw-host:dev",
        source: "config",
        setting: "ARGUS_NANOCLAW_HOST_IMAGE",
        present: true,
        detail: "present",
      };
      r.agent_image = {
        image: "nc-agent:1",
        source: "config",
        setting: "ARGUS_NANOCLAW_AGENT_IMAGE",
        present: t.id === "nanoclaw-socket" ? true : null,
        detail: "",
      };
    }
  }
  return cat;
}
