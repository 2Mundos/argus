/**
 * Path matcher aligned with OneCLI gateway 1.42 (`apps/gateway/src/inject.rs::path_matches`).
 * Used in tests only — keep in sync when upgrading the pinned OneCLI image.
 */
export function onecliPathMatches(requestPath: string, pattern: string): boolean {
  const path = requestPath.split("?")[0] ?? requestPath;
  if (pattern === "*") return true;
  if (hasMidPathWildcard(pattern)) return segmentWildcardMatches(path, pattern);
  if (pattern.endsWith("/*")) {
    const prefix = pattern.slice(0, -2);
    return path === prefix || (path.startsWith(prefix) && path.charAt(prefix.length) === "/");
  }
  if (pattern.endsWith("*")) {
    const prefix = pattern.slice(0, -1);
    return path.startsWith(prefix);
  }
  return path === pattern;
}

function hasMidPathWildcard(pattern: string): boolean {
  return pattern.length > 1 && pattern.slice(0, -1).includes("*");
}

function segmentWildcardMatches(path: string, pattern: string): boolean {
  const pathSegs = path.split("/");
  const patSegs = pattern.split("/");
  const trailingWild = patSegs.at(-1) === "*";
  const fixedPats = trailingWild ? patSegs.slice(0, -1) : patSegs;
  if (trailingWild) {
    if (pathSegs.length < fixedPats.length + 1) return false;
  } else if (pathSegs.length !== patSegs.length) return false;
  for (let i = 0; i < fixedPats.length; i++) {
    if (!segmentMatches(pathSegs[i] ?? "", fixedPats[i] ?? "")) return false;
  }
  return true;
}

function segmentMatches(segment: string, pattern: string): boolean {
  const pos = pattern.indexOf("*");
  if (pos < 0) return segment === pattern;
  const prefix = pattern.slice(0, pos);
  const suffix = pattern.slice(pos + 1);
  return (
    segment.startsWith(prefix) &&
    segment.endsWith(suffix) &&
    segment.length >= prefix.length + suffix.length
  );
}
