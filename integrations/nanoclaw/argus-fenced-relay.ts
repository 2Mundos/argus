/**
 * A fenced, bounded reader for an append-only JSON-lines file the worker container writes
 * and the host reads (progress milestones spec 3.3; reusable by other record relays).
 *
 * The file is untrusted: it lives in a directory the container can write. The reader
 * refuses a symlink or any non-regular file, reads at most `readMax` bytes per call, keeps a
 * partial trailing line until its newline arrives, drops a line over `maxLine` bytes, and
 * starts over from the top when the file shrank or was replaced. It decides nothing about
 * what a line means: validation and de-duplication belong to the caller.
 */
import { constants as fsc } from "node:fs";
import { lstat, open } from "node:fs/promises";
import path from "node:path";

export const FENCED_READ_MAX = 256 * 1024; // bytes per read
export const FENCED_LINE_MAX = 8 * 1024; // bytes per line

export interface FencedRead {
  /** Complete lines read now, in order. */
  lines: string[];
  /** Lines (or runaway partial lines) dropped for length. */
  oversized: number;
  /** The file shrank or was replaced, so reading restarted from the top. */
  restarted: boolean;
}

/** `refused` is set once the file is a link or not a plain file: the caller stops. */
export function createFencedReader(opts: { file: string; readMax?: number; maxLine?: number }) {
  const readMax = opts.readMax ?? FENCED_READ_MAX;
  const maxLine = opts.maxLine ?? FENCED_LINE_MAX;
  let offset = 0;
  let size = 0;
  let identity = "";
  let tail: Buffer = Buffer.alloc(0);
  let skipping = false; // inside a line already over the limit: discard up to its newline
  let refused: string | null = null;

  async function read(): Promise<FencedRead> {
    const result: FencedRead = { lines: [], oversized: 0, restarted: false };
    if (refused) return result;
    const dir = await lstat(path.dirname(opts.file));
    if (!dir.isDirectory()) throw new Error("relay directory is not a plain directory");
    let link: Awaited<ReturnType<typeof lstat>>;
    try {
      link = await lstat(opts.file);
    } catch (err) {
      if ((err as NodeJS.ErrnoException)?.code === "ENOENT") return result;
      throw err;
    }
    if (!link.isFile() || link.nlink !== 1) {
      refused = link.isSymbolicLink() ? "symlink" : "not_a_plain_file";
      return result;
    }
    let handle: Awaited<ReturnType<typeof open>>;
    try {
      handle = await open(opts.file, fsc.O_RDONLY | fsc.O_NOFOLLOW | fsc.O_NONBLOCK);
    } catch (err) {
      const code = (err as NodeJS.ErrnoException)?.code;
      if (code === "ENOENT") return result;
      if (code === "ELOOP") refused = "symlink";
      else throw err;
      return result;
    }
    try {
      const st = await handle.stat();
      if (!st.isFile() || st.nlink !== 1 || st.ino !== link.ino || st.dev !== link.dev) {
        refused = "not_a_plain_file"; // swapped between the check and the open
        return result;
      }
      const id = `${st.dev}:${st.ino}`;
      if ((identity && id !== identity) || st.size < offset) {
        offset = 0;
        tail = Buffer.alloc(0);
        skipping = false;
        result.restarted = true;
      }
      identity = id;
      size = st.size;
      if (st.size === offset) return result;
      const buf = Buffer.alloc(Math.min(st.size - offset, readMax));
      const { bytesRead } = await handle.read(buf, 0, buf.length, offset);
      offset += bytesRead;
      let data = Buffer.concat([tail, buf.subarray(0, bytesRead)]);
      tail = Buffer.alloc(0);
      for (;;) {
        const nl = data.indexOf(0x0a);
        if (nl < 0) break;
        const line = data.subarray(0, nl);
        data = data.subarray(nl + 1);
        if (skipping)
          skipping = false; // the end of the runaway line
        else if (line.length > maxLine) result.oversized += 1;
        else if (line.length) result.lines.push(line.toString("utf8"));
      }
      if (skipping || data.length > maxLine) {
        if (!skipping) result.oversized += 1;
        skipping = true; // discard the rest of this line as it arrives
      } else {
        tail = Buffer.from(data); // an unfinished record waits for its newline
      }
    } finally {
      await handle.close();
    }
    return result;
  }

  return {
    read,
    /** Why the file was refused (the caller should stop), or null. */
    get refused(): string | null {
      return refused;
    },
    /** Bytes in the file not read yet, as of the last read. */
    get unread(): number {
      return Math.max(0, size - offset);
    },
  };
}
