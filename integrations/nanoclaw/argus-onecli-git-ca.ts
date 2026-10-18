/**
 * Git uses its own CA store; it does not read SSL_CERT_FILE. Point GIT_SSL_CAINFO at
 * the same combined gateway trust bundle NanoClaw already mounts for Node (or the
 * gateway-only CA when system trust is unavailable).
 */
export function applyGitSslCaInfo(
  env: Record<string, string>,
  gatewayCaContainerPath: string,
): void {
  env.GIT_SSL_CAINFO = env.SSL_CERT_FILE ?? gatewayCaContainerPath;
}
