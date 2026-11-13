"""Claude Code-style conveniences for Volt Code.

* VOLT.md: project memory read into every conversation (like CLAUDE.md);
  "# note" adds a line to it.
* @path: attach a project file to your message.
* !command: run a shell command yourself; its output joins the conversation.
* Tool labels: Read(...), Update(...), Bash(...) as Claude Code shows them.
* Permission modes: ask (default), accept edits, plan (read-only).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

MEMORY_FILE = "VOLT.md"
MAX_MEMORY_CHARS = 20_000
MAX_ATTACH_CHARS = 60_000
SHELL_TIMEOUT_SEC = 120

LABELS = {
    "read_file": "Read", "write_file": "Write", "edit_file": "Update",
    "list_files": "List", "search": "Search", "run_command": "Bash",
}

MODES = ("default", "accept_edits", "plan")
MODE_TEXT = {
    "default": "",
    "accept_edits": "⏵⏵ accept edits on",
    "plan": "⏸ plan mode on (read-only)",
}
PLAN_PROMPT = (
    "\n\nPlan mode is ON: only look at the project (read, list, search). Do not change files "
    "or run commands. Reply with a short, numbered plan the user can approve."
)
INIT_REQUEST = (
    "Please analyse this codebase and create a VOLT.md file in the project folder. It is read "
    "into every future conversation, so keep it short and useful: what the project is, how to "
    "install, build, run and test it (exact commands), the folder layout, and the code style "
    "and conventions you see. Look at the files first, then write VOLT.md with write_file."
)
COMPACT_REQUEST = (
    "Summarise our conversation so far so it can replace the full history: the goal, what "
    "was done (files changed, commands run and their results), decisions made, and what is "
    "left to do. Be specific and brief. Reply with the summary only."
)


# --- Project memory -----------------------------------------------------------------

def memory_path(root: Path) -> Path:
    return Path(root) / MEMORY_FILE


def load_project_memory(root: Path) -> str:
    try:
        return memory_path(root).read_text(errors="replace")[:MAX_MEMORY_CHARS]
    except OSError:
        return ""


def remember(root: Path, note: str) -> Path:
    """Add one line to VOLT.md (creating it), as Claude Code's "# note" does for CLAUDE.md."""
    path = memory_path(root)
    existing = path.read_text(errors="replace") if path.exists() else "# Project notes for Volt Code\n"
    if not existing.endswith("\n"):
        existing += "\n"
    path.write_text(existing + f"- {note.strip()}\n")
    return path


# --- @file mentions -------------------------------------------------------------------

MENTION = re.compile(r"(?<![\w@])@([^\s@]+)")


def expand_mentions(text: str, root: Path) -> tuple[str, list[str]]:
    """Attach the contents of each @path that is a file inside the project."""
    root = Path(root).resolve()
    attached, blocks = [], []
    for raw in MENTION.findall(text):
        candidate = raw.rstrip(".,;:!?)")
        path = Path(candidate).expanduser()
        path = (path if path.is_absolute() else root / path).resolve()
        if root not in path.parents or not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in attached:
            continue
        try:
            content = path.read_text(errors="replace")[:MAX_ATTACH_CHARS]
        except OSError:
            continue
        attached.append(rel)
        blocks.append(f'<file path="{rel}">\n{content}\n</file>')
    if not blocks:
        return text, []
    return text + "\n\nAttached files:\n" + "\n".join(blocks), attached


# --- !shell ----------------------------------------------------------------------------

def run_shell(command: str, root: Path) -> str:
    shell = (["powershell", "-NoProfile", "-Command", command] if sys.platform == "win32"
             else ["bash", "-lc", command])
    try:
        done = subprocess.run(shell, cwd=root, capture_output=True, text=True,
                              timeout=SHELL_TIMEOUT_SEC, errors="replace")
    except subprocess.TimeoutExpired:
        return f"(stopped after {SHELL_TIMEOUT_SEC} seconds)"
    out = (done.stdout + ("\n" + done.stderr if done.stderr else "")).strip()
    return (out[-20_000:] if out else "(no output)") + f"\n(exit code {done.returncode})"


# --- Display ----------------------------------------------------------------------------

def tool_label(call) -> str:
    args = call.input if isinstance(call.input, dict) else {}
    if call.toolset:
        detail = args.get("text") or args.get("coordinate") or args.get("region") or ""
        return f"Computer({call.name}{' ' + str(detail) if detail else ''})"
    detail = args.get("path") or args.get("pattern") or args.get("command") or ""
    return f"{LABELS.get(call.name, call.name)}({str(detail)[:100]})"


# --- Permission modes ---------------------------------------------------------------------

def next_mode(mode: str) -> str:
    return MODES[(MODES.index(mode) + 1) % len(MODES)] if mode in MODES else "default"


def mode_decides(mode: str, title: str) -> Optional[bool]:
    """True/False when the mode settles an approval by itself; None to ask the user."""
    is_command = title.startswith("Run")
    is_screen = title.startswith("Control the screen")
    if mode == "plan":
        return False
    if mode == "accept_edits" and not is_command and not is_screen:
        return True
    return None
