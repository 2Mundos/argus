export const DEFAULT_PROJECT = "Global";

/** "radar-web" → "Radar Web"; empty/missing → "Global". */
export function humanizeProject(slug: string | null | undefined): string {
  const words = (slug ?? "").split(/[-_\s]+/).filter(Boolean);
  if (words.length === 0) return DEFAULT_PROJECT;
  return words.map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(" ");
}

/** "<Project> - <title>" as plain text. */
export function taskLabelText(title: string, project: string | null | undefined): string {
  return `${humanizeProject(project)} - ${title}`;
}

/** A task title prefixed with its project; the full text is kept in `title` for truncated layouts. */
export function TaskLabel({
  title,
  project,
  className,
}: {
  title: string;
  project?: string | null;
  className?: string;
}) {
  const text = taskLabelText(title, project);
  return (
    <span className={className} title={text}>
      {text}
    </span>
  );
}
