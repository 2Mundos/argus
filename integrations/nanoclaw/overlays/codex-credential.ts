import fs from "fs";
import path from "path";

/** The refusal's name: it leads the error, so Argus classifies it (failure classes 3.2). */
export const CODEX_CREDENTIAL_MISSING = "codex_credential_missing";

export class CodexCredentialMissingError extends Error {
  constructor(authPath: string) {
    super(
      `${CODEX_CREDENTIAL_MISSING}: no Codex credential at ${authPath}; ` +
        "the gateway serves it as a sentinel auth.json when the agent holds an openai lease",
    );
    this.name = "CodexCredentialMissingError";
  }
}

/**
 * Make sure the child's CODEX_HOME exists, then refuse when the gateway's sentinel
 * `auth.json` is not in it (codex-accounts spec): the CLI would otherwise start, find
 * no credential and exit 0. Only presence is checked; the file is never read, and no
 * credential is put in the child's environment.
 */
export function requireCodexCredential(childEnv: NodeJS.ProcessEnv): void {
  const home = childEnv.CODEX_HOME || path.join(childEnv.HOME || "/home/node", ".codex");
  fs.mkdirSync(home, { recursive: true });
  const authPath = path.join(home, "auth.json");
  let usable = false;
  try {
    const st = fs.statSync(authPath);
    usable = st.isFile() && st.size > 0;
  } catch {
    // absent: refused below
  }
  if (!usable) throw new CodexCredentialMissingError(authPath);
}
