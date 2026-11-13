"""Arrow-key list picker for the terminal (used by /resume).

↑/↓ (or k/j) move, PgUp/PgDn jump, typing filters the list, Backspace edits
the filter, Enter picks, Esc or Ctrl+C cancels. Works on Linux, macOS and
Windows; falls back to typing a number when there is no real terminal.
"""

from __future__ import annotations

import os
import sys
from typing import Callable, Optional

UP, DOWN, PGUP, PGDN, ENTER, ESC, BACKSPACE = "up", "down", "pgup", "pgdn", "enter", "esc", "backspace"
VISIBLE = 10


def read_key() -> str:
    """One key press as a name (UP, ENTER, ...) or the typed character."""
    if os.name == "nt":
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return {"H": UP, "P": DOWN, "I": PGUP, "Q": PGDN}.get(msvcrt.getwch(), "")
        return {"\r": ENTER, "\x1b": ESC, "\x08": BACKSPACE, "\x03": ESC}.get(ch, ch)
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = os.read(fd, 1).decode(errors="ignore")
        if ch == "\x1b":
            import select

            if not select.select([fd], [], [], 0.05)[0]:
                return ESC
            seq = os.read(fd, 5).decode(errors="ignore")
            return {"[A": UP, "[B": DOWN, "[5~": PGUP, "[6~": PGDN, "OA": UP, "OB": DOWN}.get(seq, "")
        return {"\r": ENTER, "\n": ENTER, "\x7f": BACKSPACE, "\x08": BACKSPACE, "\x03": ESC}.get(ch, ch)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


class Picker:
    """The picker's state, separate from drawing and key reading (testable)."""

    def __init__(self, rows: list[str], search: list[str]):
        self.rows, self.search = rows, [s.lower() for s in search]
        self.query, self.pos, self.top = "", 0, 0

    @property
    def matches(self) -> list[int]:
        q = self.query.lower()
        return [i for i, s in enumerate(self.search) if q in s]

    def press(self, key: str) -> Optional[str]:
        """Apply a key; returns "pick", "cancel" or None to keep going."""
        count = len(self.matches)
        if key == ESC:
            return "cancel"
        if key == ENTER:
            return "pick" if count else None
        if key == UP or (key == "k" and not self.query):
            self.pos = max(0, self.pos - 1)
        elif key == DOWN or (key == "j" and not self.query):
            self.pos = min(count - 1, self.pos + 1)
        elif key == PGUP:
            self.pos = max(0, self.pos - VISIBLE)
        elif key == PGDN:
            self.pos = min(count - 1, self.pos + VISIBLE)
        elif key == BACKSPACE:
            self.query = self.query[:-1]
            self.pos = 0
        elif len(key) == 1 and key.isprintable():
            self.query += key
            self.pos = 0
        self.pos = max(0, min(self.pos, len(self.matches) - 1))
        self.top = min(max(self.top, self.pos - VISIBLE + 1), self.pos)
        return None

    @property
    def chosen(self) -> Optional[int]:
        m = self.matches
        return m[self.pos] if m else None


def choose(title: str, header: str, rows: list[str], search: Optional[list[str]] = None,
           paint: Callable[[str, str], str] = lambda code, text: text) -> Optional[int]:
    """Let the user pick one row; returns its index or None."""
    if not rows:
        return None
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(title)
        for n, row in enumerate(rows, 1):
            print(f"  {n:>2}. {row}")
        answer = input("  Number (Enter to cancel): ").strip()
        return int(answer) - 1 if answer.isdigit() and 1 <= int(answer) <= len(rows) else None

    state = Picker(rows, search or rows)
    drawn = 0
    out = sys.stdout
    while True:
        if drawn:
            out.write(f"\x1b[{drawn}F\x1b[J")  # back to the top of the last drawing, clear below
        lines = [paint("1", title), paint("2", header)]
        matches = state.matches
        for i in matches[state.top:state.top + VISIBLE]:
            selected = matches[state.pos] == i if matches else False
            lines.append(paint("38;5;209;1", "❯ " + rows[i]) if selected else "  " + rows[i])
        if not matches:
            lines.append(paint("2", "  No chats match."))
        more = len(matches) - state.top - VISIBLE
        footer = f"  ↑/↓ move · type to search{f' ({state.query})' if state.query else ''} · Enter resume · Esc cancel"
        lines.append(paint("2", footer + (f" · {more} more" if more > 0 else "")))
        out.write("\n".join(lines) + "\n")
        out.flush()
        drawn = len(lines)
        result = state.press(read_key())
        if result == "cancel":
            out.write(f"\x1b[{drawn}F\x1b[J")
            return None
        if result == "pick":
            out.write(f"\x1b[{drawn}F\x1b[J")
            return state.chosen
