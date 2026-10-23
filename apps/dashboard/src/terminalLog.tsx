import type { ReactNode } from "react";

/** Basic SGR foreground classes for CSI color codes. */
const FG: Record<number, string> = {
  30: "term-fg-black",
  31: "term-fg-red",
  32: "term-fg-green",
  33: "term-fg-yellow",
  34: "term-fg-blue",
  35: "term-fg-magenta",
  36: "term-fg-cyan",
  37: "term-fg-white",
  90: "term-fg-bright-black",
  91: "term-fg-bright-red",
  92: "term-fg-bright-green",
  93: "term-fg-bright-yellow",
  94: "term-fg-bright-blue",
  95: "term-fg-bright-magenta",
  96: "term-fg-bright-cyan",
  97: "term-fg-bright-white",
};

const CSI = /\x1b\[([0-9;]*)m/g;

export type TermSeg = { text: string; className?: string };

/** Split one line into colored segments; strips other CSI/OSC noise. */
export function parseAnsiLine(raw: string): TermSeg[] {
  const line = raw
    .replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, "")
    .replace(/\x1b\[[0-9;?]*[A-Za-z]/g, (m) => (m.endsWith("m") ? m : ""));
  const segs: TermSeg[] = [];
  let className: string | undefined;
  let last = 0;
  CSI.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = CSI.exec(line)) !== null) {
    if (match.index > last) {
      segs.push({ text: line.slice(last, match.index), className });
    }
    const codes = (match[1] || "0").split(";").map((n) => Number(n) || 0);
    for (const code of codes) {
      if (code === 0) className = undefined;
      else if (code === 1) className = [className, "term-bold"].filter(Boolean).join(" ");
      else if (code === 2) className = [className, "term-dim"].filter(Boolean).join(" ");
      else if (FG[code]) className = FG[code];
    }
    last = match.index + match[0].length;
  }
  if (last < line.length) segs.push({ text: line.slice(last), className });
  if (segs.length === 0) segs.push({ text: "", className });
  return segs;
}

/** Heuristic tone when the line carries no ANSI colors. */
export function lineTone(line: string): string | undefined {
  if (/\x1b\[/.test(line)) return undefined;
  if (/\b(ERROR|FATAL|CRITICAL|panic|Traceback|Exception)\b/i.test(line)) return "term-fg-red";
  if (/\b(WARN(ING)?|DEPRECATED)\b/i.test(line)) return "term-fg-yellow";
  if (/\b(DEBUG|TRACE)\b/i.test(line)) return "term-dim";
  if (/\b(INFO|NOTICE)\b/i.test(line)) return "term-fg-cyan";
  return undefined;
}

/** Render plain log text as terminal lines (ANSI + heuristic tones). */
export function renderTerminalText(text: string): ReactNode {
  const lines = text.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n");
  // A trailing empty split from a final newline is noise.
  if (lines.length > 0 && lines[lines.length - 1] === "") lines.pop();
  return lines.map((line, i) => {
    const tone = lineTone(line);
    const segs = parseAnsiLine(line);
    return (
      <div key={i} className={tone ? `term-line ${tone}` : "term-line"}>
        {segs.map((seg, j) =>
          seg.className ? (
            <span key={j} className={seg.className}>
              {seg.text || "\u00a0"}
            </span>
          ) : (
            <span key={j}>{seg.text || (segs.length === 1 ? "\u00a0" : "")}</span>
          ),
        )}
      </div>
    );
  });
}
