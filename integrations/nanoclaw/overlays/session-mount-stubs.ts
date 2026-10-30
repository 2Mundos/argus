/**
 * Pre-create the nested bind-mount points and the image WORKDIR inside a
 * session directory as the host user.
 *
 * The OCI runtime runs as root. When `sessDir/agent` (group folder mount) or
 * `sessDir/group` (image WORKDIR) is missing, Docker creates them on the host
 * as `0:0` mode `0755`. The host uid can then never empty or remove them, and
 * workspace cleanup blocks with `permission_denied` (docs/specs/workspace-cleanup.spec.md
 * 3.3). Creating them here, before `docker run`, makes the runtime reuse the
 * host-owned directories instead.
 *
 * Never chown. Never chmod or otherwise mutate a path that already exists.
 */
import fs from "node:fs";
import path from "node:path";

const STUB_MODE = 0o755;
const STUB_NAMES = ["agent", "group"] as const;

export function ensureSessionMountStubs(sessionDir: string): void {
  for (const name of STUB_NAMES) {
    const dir = path.join(sessionDir, name);
    try {
      // lstat, not stat: an existing symlink is left alone rather than followed.
      fs.lstatSync(dir);
      // Any existing path, directory or not, is never mutated. A non-directory
      // fails closed in the runtime's own bind/workdir setup.
      continue;
    } catch (err) {
      const code = (err as NodeJS.ErrnoException).code;
      if (code !== "ENOENT") throw err;
    }
    fs.mkdirSync(dir, { recursive: true, mode: STUB_MODE });
    // mkdir mode is masked by umask; set the intended mode only on create.
    fs.chmodSync(dir, STUB_MODE);
  }
}
