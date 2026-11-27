import type { MouseEvent } from "react";

const DEFAULT_PROJECT = "Global";

/** "radar-web" → "Radar Web"; empty/missing → "Global". */
export function humanizeProject(slug: string | null | undefined): string {
  const words = (slug ?? "").split(/[-_\s]+/).filter(Boolean);
  if (words.length === 0) return DEFAULT_PROJECT;
  return words.map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(" ");
}

/** Project slug for links; empty/missing → "global". */
export function projectSlug(project: string | null | undefined): string {
  const slug = (project ?? "").trim();
  return slug || "global";
}

/** `#projects/<slug>/graph` — kept here so TaskLabel does not import Projects. */
export function projectGraphHash(project: string | null | undefined): string {
  return `#projects/${encodeURIComponent(projectSlug(project))}/graph`;
}

/** Plain-text label used in tooltips and aria text. */
export function taskLabelText(title: string, project: string | null | undefined): string {
  return `${humanizeProject(project)} · ${title}`;
}

/** Project chip linking to the project's task graph. */
export function ProjectChip({ project }: { project?: string | null }) {
  const name = humanizeProject(project);
  const href = projectGraphHash(project);
  // Task cards are buttons: a nested link must not open the card.
  function openGraph(e: MouseEvent<HTMLAnchorElement>) {
    e.preventDefault();
    e.stopPropagation();
    window.location.hash = href;
  }
  return (
    <a
      className="badge project-chip"
      href={href}
      title={`Open ${name} task graph`}
      aria-label={`Open ${name} task graph`}
      onClick={openGraph}
    >
      {name}
    </a>
  );
}

/** Project chip (optional) plus the task title. */
export function TaskLabel({
  title,
  project,
  className,
  projectChip = true,
}: {
  title: string;
  project?: string | null;
  className?: string;
  /** When false, only the title is shown (the chip lives elsewhere). */
  projectChip?: boolean;
}) {
  return (
    <span className={["task-label", className].filter(Boolean).join(" ")} title={title}>
      {projectChip ? (
        <>
          <ProjectChip project={project} />{" "}
        </>
      ) : null}
      <span className="task-label-title">{title}</span>
    </span>
  );
}
