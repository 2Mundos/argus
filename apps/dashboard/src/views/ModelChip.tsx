import { ConfiguredIcon, ProviderMark, providerForTool } from "./providerIcons";

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
      <span className="model-chip-id">{id}</span>
      {configured ? <ConfiguredIcon /> : null}
    </span>
  );
}
