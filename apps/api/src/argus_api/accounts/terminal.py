"""What a terminal would show for a client's output: just enough of a VT100 to
replay a TUI that redraws by moving the cursor (claude accounts spec 3).

The Claude client's renderer only writes the cells that changed since its last
frame and jumps over the rest (`ESC[10G`), so stripping escape codes from the
stream drops characters that were already on screen. Replaying the stream on a
grid gives the text the operator would read. Unknown sequences are ignored; the
result is used for parsing only, never logged or returned.
"""

import re

_SEQ = re.compile(
    r"\x1b\[([0-?]*)[ -/]*([@-~])"  # CSI: parameters, final byte
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC (titles, hyperlinks)
    r"|\x1b[()*+].?"  # charset designation
    r"|\x1b(.)"  # other two-byte escapes (save/restore cursor among them)
    r"|([\x00-\x1f\x7f])"  # C0 controls
    r"|([^\x00-\x1f\x7f\x1b]+)",  # printable text
    re.S,
)


def _num(params: str, default: int = 1) -> int:
    head = params.split(";", 1)[0]
    return int(head) if head.isdigit() and int(head) > 0 else default


def screen(raw: bytes, columns: int, rows: int) -> list[str]:
    """The lines a `columns` x `rows` terminal shows after `raw`, oldest first
    (scrolled-off lines are kept), trailing blanks trimmed."""
    text = raw.decode("utf-8", "replace")
    grid: list[list[str]] = [[]]
    r = c = 0
    saved = (0, 0)

    def top() -> int:  # first line of the visible screen
        return max(0, len(grid) - rows)

    def goto(row: int, col: int) -> None:
        nonlocal r, c
        r, c = max(top(), row), min(max(0, col), columns - 1)
        while len(grid) <= r:
            grid.append([])

    def put(ch: str) -> None:
        nonlocal c
        if c >= columns:  # autowrap
            goto(r + 1, 0)
        line = grid[r]
        if len(line) <= c:
            line.extend(" " * (c - len(line) + 1))
        line[c] = ch
        c += 1

    for m in _SEQ.finditer(text):
        params, final, esc, ctl, chars = m.groups()
        if chars is not None:
            for ch in chars:
                put(ch)
        elif ctl is not None:
            if ctl == "\r":
                c = 0
            elif ctl == "\n":
                goto(r + 1, c)
            elif ctl == "\b":
                c = max(0, c - 1)
        elif esc is not None:
            if esc == "7":
                saved = (r - top(), c)
            elif esc == "8":
                goto(top() + saved[0], saved[1])
        elif final is not None and not params.startswith(("?", "<", ">", "=")):
            # (private sequences, e.g. `ESC[?u` keyboard queries, draw nothing)
            n = _num(params)
            line = grid[r]
            if final == "A":
                goto(r - n, c)
            elif final in "Be":
                goto(r + n, c)
            elif final in "Ca":
                goto(r, c + n)
            elif final == "D":
                goto(r, c - n)
            elif final == "E":
                goto(r + n, 0)
            elif final == "F":
                goto(r - n, 0)
            elif final in "G`":
                goto(r, n - 1)
            elif final in "Hf":
                parts = [*params.split(";"), "", ""][:2]
                goto(top() + _num(parts[0]) - 1, _num(parts[1]) - 1)
            elif final == "d":
                goto(top() + n - 1, c)
            elif final == "K":
                mode = _num(params, 0)
                if mode == 0:
                    del line[c:]
                elif mode == 1:
                    line[: c + 1] = " " * min(len(line), c + 1)
                else:
                    line.clear()
            elif final == "J":
                mode = _num(params, 0)
                if mode == 0:
                    del line[c:]
                    del grid[r + 1 :]
                elif mode == 1:
                    for i in range(top(), r):
                        grid[i].clear()
                    line[: c + 1] = " " * min(len(line), c + 1)
                else:
                    for i in range(top(), len(grid)):
                        grid[i].clear()
            elif final == "X":
                end = min(len(line), c + n)
                line[c:end] = " " * (end - c) if end > c else []
            elif final == "P":
                del line[c : c + n]
            elif final == "@":
                line[c:c] = " " * n
            elif final == "s":
                saved = (r - top(), c)
            elif final == "u":
                goto(top() + saved[0], saved[1])
            # SGR (m), modes (h/l), scroll region (r) and the rest draw nothing.
    return ["".join(line).rstrip() for line in grid]
