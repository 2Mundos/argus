/** At most this many characters of a snippet are shown, whatever the server sent. */
export const SNIPPET_MAX = 200;

export function boundedSnippet(snippet: string): string {
  return snippet.length > SNIPPET_MAX ? `${snippet.slice(0, SNIPPET_MAX)}…` : snippet;
}
