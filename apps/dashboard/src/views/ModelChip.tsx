import { ConfiguredIcon, ProviderMark, providerForTool } from "./providerIcons";

/** `claude-haiku-5-5` -> `Haiku 5.5`; ids that do not fit the pattern are shown as-is. */
export function shortModelName(id: string): string {
  const m = id.match(/^(?:claude-|gpt-)?([a-z]+)-(\d+)[-.](\d+)$/);
  if (!m) return id;
  const [, name = "", major, minor] = m;
  return `${name.charAt(0).toUpperCase()}${name.slice(1)} ${major}.${minor}`;
}

/** Compact chip: provider mark + model id; gear means configured, not yet observed. */
export function ModelChip({
  tool,
  model,
  observed = true,
  detail,
}: {
  tool: string | null | undefined;
  model: string | null | undefined;
  observed?: boolean;
  /** Extra tooltip facts (evidence, layer, …). */
  detail?: string | null;
}) {
  const mark = tool ?? "unknown";
  const id = model ?? "?";
  const configured = !observed && !!model;
  const title = [mark, id, configured ? "configured" : null, detail].filter(Boolean).join(" · ");
  return (
    <span className="badge model-chip" title={title}>
      <ProviderMark provider={providerForTool(mark)} />
      <span className="model-chip-id">{shortModelName(id)}</span>
      {configured ? <ConfiguredIcon /> : null}
    </span>
  );
}
