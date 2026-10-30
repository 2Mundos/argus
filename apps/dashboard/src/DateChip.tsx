import { relative, utcIso } from "./format";

/** Relative time chip; hover shows the UTC ISO timestamp. */
export function DateChip(props: {
  at: string | null | undefined;
  now?: number;
  className?: string;
}) {
  const { at, now, className } = props;
  const utc = utcIso(at);
  if (!utc) {
    return <span className={["date-chip", "empty", className].filter(Boolean).join(" ")}>—</span>;
  }
  return (
    <time className={["date-chip", className].filter(Boolean).join(" ")} dateTime={utc} title={utc}>
      {relative(at, now)}
    </time>
  );
}
