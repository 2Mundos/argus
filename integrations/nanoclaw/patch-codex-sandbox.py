#!/usr/bin/env python3
"""Patch a NanoClaw checkout for the per-worker Codex sandbox profile (called by install.sh).

usage: patch-codex-sandbox.py /path/to/nanoclaw

Idempotent AND upgrading: every hook is "ensure this exact text". A hook is applied to a
pristine checkout, rewritten if an earlier version of it (v1) is present, and left alone if
current, so a second run changes nothing and an already-installed checkout converges.
"""

import sys
from pathlib import Path

root = Path(sys.argv[1])


def ensure(text, what, new, *, anchor=None, replace=(), insert=None):
    """new: final text. replace: older texts to upgrade from. insert: (old, new_with_old) on a pristine tree."""
    if new in text:
        return text
    for older in replace:
        if older in text:
            return text.replace(older, new, 1)
    old, full = insert
    assert text.count(old) == 1, f"{what}: upstream shape changed"
    return text.replace(old, full, 1)


def edit(path, steps):
    p = root / path
    if not p.is_file():
        return
    s = p.read_text()
    original = s
    for step in steps:
        s = ensure(s, *step[:2], **step[2])
    if s != original:
        p.write_text(s)
        print(f"patched {p}")


# ---- drivers/types.ts -------------------------------------------------------------------------
FIELD = """  labels?: Record<string, string>;
  /**
   * A closed token, never a flag: the Docker driver maps it to fixed AppArmor and
   * seccomp options (drivers/argus-codex-sandbox.ts). Agent role only.
   */
  securityProfile?: 'codex-bwrap';
  // No raw runtime flags here"""
CHECK_V1 = """    if (
      container.securityProfile !== undefined &&
      (container.securityProfile !== 'codex-bwrap' || container.role !== 'agent')
    ) {
      throw deniedByPolicy(`security profile '${String(container.securityProfile)}' is not allowed on role ${container.role}`);
    }
"""
CHECK_V2 = """    if (container.securityProfile !== undefined) {
      if (container.securityProfile !== 'codex-bwrap' || container.role !== 'agent') {
        throw deniedByPolicy(`security profile '${String(container.securityProfile)}' is not allowed on role ${container.role}`);
      }
      if (!capabilities?.securityProfiles?.includes(container.securityProfile)) {
        throw deniedByPolicy(
          `this driver cannot realize security profile '${container.securityProfile}'; refusing instead of running without it`,
        );
      }
    }
"""
LOOP = "  for (const container of spec.containers) {\n    const seenTargets = new Set<string>();"
CAP_FIELD = """  imageBuild: boolean;
  /** Security profile tokens this driver realizes; absent means none. A spec asking for one is refused otherwise. */
  securityProfiles?: readonly 'codex-bwrap'[];
}"""
edit(
    "src/drivers/types.ts",
    [
        (
            "ContainerSpec.securityProfile",
            FIELD,
            {
                "insert": (
                    "  labels?: Record<string, string>;\n  // No raw runtime flags here",
                    FIELD,
                )
            },
        ),
        (
            "validateSpec security profile check",
            CHECK_V2 + "    const seenTargets = new Set<string>();",
            {
                "replace": (CHECK_V1 + "    const seenTargets = new Set<string>();",),
                "insert": (
                    LOOP,
                    "  for (const container of spec.containers) {\n"
                    + CHECK_V2
                    + "    const seenTargets = new Set<string>();",
                ),
            },
        ),
        (
            "DriverCapabilities.securityProfiles",
            CAP_FIELD,
            {"insert": ("  imageBuild: boolean;\n}", CAP_FIELD)},
        ),
    ],
)

# ---- drivers/docker-driver.ts -----------------------------------------------------------------
IMPORT_V1 = """import {
  assertDaemonSupportsProfile,
  defaultSeccompSource,
  securityProfileArgs,
  type SeccompSource,
} from './argus-codex-sandbox.js';
import { JsonDocumentStream } from './json-stream.js';"""
IMPORT_V2 = """import { CodexSandboxGuard, type SeccompSource } from './argus-codex-sandbox.js';
import { JsonDocumentStream } from './json-stream.js';"""
OPTS_V1 = """  /** Test seam for the Codex sandbox seccomp file; production uses the fixed install path. */
  seccompSource?: SeccompSource;
}"""
OPTS_V2 = """  /** Test seams for the Codex sandbox: the seccomp file (production: the fixed install path) and the clock. */
  seccompSource?: SeccompSource;
  now?: () => number;
}"""
METHOD_V1 = """  #securityArgs(agent: ContainerSpec): string[] {
    if (!agent.securityProfile) return [];
    let info: string;
    try {
      info = this.#cli.run(['info', '--format', '{{json .SecurityOptions}}'], { timeoutMs: 10_000 });
    } catch (error) {
      throw normalizeDockerError(error);
    }
    assertDaemonSupportsProfile(info);
    return securityProfileArgs(agent.securityProfile, this.opts.seccompSource ?? defaultSeccompSource());
  }
"""
DOC = """  /**
   * Flags for the agent's opted-in security profile. Every refusal is a denial: no
   * fallback to the default profile, and nothing is created.
   */
"""
METHOD_V1 = DOC + METHOD_V1
METHOD_V2 = (
    """  #guard(): CodexSandboxGuard {
    return new CodexSandboxGuard(this.#cli, this.opts, normalizeDockerError);
  }

"""
    + DOC
    + """  #securityArgs(agent: ContainerSpec): string[] {
    return agent.securityProfile ? this.#guard().args(agent) : [];
  }
"""
)
ADOPT_OLD = "    if (this.#existingSession(name, spec.key)) {\n      return new DockerHandle(spec.key, name, this.#cli, null, auxiliaryNames, privateNetwork, this.#emit);"
ADOPT_NEW = "    if (this.#existingSession(name, spec.key)) {\n      // An opted-in spec adopts only a container really created with the profile (and only while it enforces).\n      if (agent.securityProfile) this.#guard().assertAdoptable(name, agent);\n      return new DockerHandle(spec.key, name, this.#cli, null, auxiliaryNames, privateNetwork, this.#emit);"
CAPS_OLD = "      imageBuild: true,\n    };"
CAPS_NEW = "      imageBuild: true,\n      securityProfiles: ['codex-bwrap'],\n    };"
edit(
    "src/drivers/docker-driver.ts",
    [
        (
            "imports",
            IMPORT_V2,
            {
                "replace": (IMPORT_V1,),
                "insert": (
                    "import { JsonDocumentStream } from './json-stream.js';",
                    IMPORT_V2,
                ),
            },
        ),
        (
            "DockerDriverOptions",
            OPTS_V2,
            {
                "replace": (OPTS_V1,),
                "insert": (
                    "  reconcileNetworkAccess?: (access: NetworkAccessIntent) => void;\n}",
                    "  reconcileNetworkAccess?: (access: NetworkAccessIntent) => void;\n"
                    + OPTS_V2,
                ),
            },
        ),
        (
            "security args method",
            METHOD_V2,
            {
                "replace": (METHOD_V1,),
                "insert": (
                    "  async prepare(spec: SessionSpec): Promise<SessionHandle> {",
                    METHOD_V2
                    + "\n  async prepare(spec: SessionSpec): Promise<SessionHandle> {",
                ),
            },
        ),
        ("adoption hook", ADOPT_NEW, {"insert": (ADOPT_OLD, ADOPT_NEW)}),
        (
            "mount check",
            "    const securityArgs = this.#securityArgs(agent);\n    for (const container of spec.containers) assertMountSourcesExist(container.mounts);\n",
            {
                "insert": (
                    "    for (const container of spec.containers) assertMountSourcesExist(container.mounts);\n",
                    "    const securityArgs = this.#securityArgs(agent);\n    for (const container of spec.containers) assertMountSourcesExist(container.mounts);\n",
                )
            },
        ),
        (
            "agent create",
            "containerCreateArgs(spec, agent, name, privateNetwork ? ['--network', privateNetwork] : networkArgs, securityArgs),",
            {
                "insert": (
                    "containerCreateArgs(spec, agent, name, privateNetwork ? ['--network', privateNetwork] : networkArgs),",
                    "containerCreateArgs(spec, agent, name, privateNetwork ? ['--network', privateNetwork] : networkArgs, securityArgs),",
                )
            },
        ),
        (
            "containerCreateArgs signature",
            "  networkArgs: readonly string[],\n  securityArgs: readonly string[] = [],\n): string[] {\n  const args = ['create', '--rm', '--name', name];",
            {
                "insert": (
                    "  networkArgs: readonly string[],\n): string[] {\n  const args = ['create', '--rm', '--name', name];",
                    "  networkArgs: readonly string[],\n  securityArgs: readonly string[] = [],\n): string[] {\n  const args = ['create', '--rm', '--name', name];",
                )
            },
        ),
        (
            "hardening call",
            "  args.push(...hardeningArgs(spec));\n  args.push(...securityArgs);\n",
            {
                "insert": (
                    "  args.push(...hardeningArgs(spec));\n",
                    "  args.push(...hardeningArgs(spec));\n  args.push(...securityArgs);\n",
                )
            },
        ),
        ("capabilities", CAPS_NEW, {"insert": (CAPS_OLD, CAPS_NEW)}),
    ],
)

# ---- container-runner.ts ----------------------------------------------------------------------
IMP_V1 = (
    "import { securityProfileForProvider } from './drivers/argus-codex-sandbox.js';"
)
IMP_V2 = "import { assertDriverSupportsSecurityProfiles, securityProfileForProvider } from './drivers/argus-codex-sandbox.js';"
COMPOSE = """  // Codex sandbox (ARGUS_CODEX_SANDBOX_PROFILE; default codex-bwrap, off to disable):
  // native Codex provider's AGENT container only, never an auxiliary one. Throws on unknown values.
  const securityProfile = securityProfileForProvider(
    resolveProviderName(session.agent_provider, containerConfig.provider),
    process.env,
  );
  if (securityProfile) agent.securityProfile = securityProfile;

"""
GATE = "    assertDriverSupportsSecurityProfiles(driver.capabilities(), spec, driver.kind);\n\n    log.info('Spawning session',"
edit(
    "src/container-runner.ts",
    [
        (
            "provider-name import",
            IMP_V2,
            {
                "replace": (IMP_V1,),
                "insert": (
                    "import { resolveProviderName } from './providers/provider-name.js';",
                    IMP_V2
                    + "\n"
                    + "import { resolveProviderName } from './providers/provider-name.js';",
                ),
            },
        ),
        (
            "composeSessionSpec hook",
            COMPOSE,
            {
                "insert": (
                    "  // The folder label (D9) rides the spec",
                    COMPOSE + "  // The folder label (D9) rides the spec",
                )
            },
        ),
        (
            "spawn capability gate",
            GATE,
            {"insert": ("    log.info('Spawning session',", GATE)},
        ),
    ],
)
