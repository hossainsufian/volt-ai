"""The tools Volt Code's brain can use inside one project folder.

Every path is resolved inside the project folder; anything outside it is
refused. Writing, editing and running commands call ``approve(title, detail)``
first and do nothing unless it returns True.
"""

from __future__ import annotations

import difflib
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

MAX_READ_LINES = 2000
MAX_OUTPUT_CHARS = 12000
MAX_LIST = 300
MAX_MATCHES = 100
COMMAND_TIMEOUT_SEC = 180
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", "dist", "build"}

Approve = Callable[[str, str], bool]


class ToolError(Exception):
    """A tool call that could not be done; the message goes back to the brain."""


# Tool descriptions in a neutral shape; each brain converts them to its API's format.
TOOLS = [
    {
        "name": "read_file",
        "description": "Read a text file in the project. Returns numbered lines. Use offset/limit for long files.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the project folder"},
                "offset": {"type": "integer", "description": "First line to read (1-based)"},
                "limit": {"type": "integer", "description": "How many lines to read"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Create a file or replace its whole content. Prefer edit_file for changes to existing files.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the project folder"},
                "content": {"type": "string", "description": "The complete file content"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": ("Replace one exact piece of text in a file. old_text must appear exactly once; "
                        "include enough surrounding lines to make it unique. Read the file first."),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string", "description": "Exact text to replace"},
                "new_text": {"type": "string", "description": "Text to put in its place"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "list_files",
        "description": "List project files matching a glob pattern such as **/*.py (default: all files).",
        "parameters": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "search",
        "description": "Search the project's text files for a regular expression. Returns path:line: text.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression"},
                "path": {"type": "string", "description": "Folder or file to search (default: whole project)"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "run_command",
        "description": ("Run a shell command in the project folder (tests, builds, git, installs). "
                        "Returns its output and exit code. Times out after 3 minutes."),
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
]


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    return f"{text[:half]}\n… ({len(text) - MAX_OUTPUT_CHARS} characters cut) …\n{text[-half:]}"


class Toolbox:
    def __init__(self, root: Path, approve: Approve):
        self.root = Path(root).resolve()
        self.approve = approve

    # --- helpers ------------------------------------------------------------

    def _path(self, value) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise ToolError("A file path is required.")
        path = Path(value).expanduser()
        path = (path if path.is_absolute() else self.root / path).resolve()
        if path != self.root and self.root not in path.parents:
            raise ToolError(f"{value} is outside the project folder {self.root}.")
        return path

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix() if path != self.root else "."

    @staticmethod
    def _text(args: dict, key: str, required: bool = True) -> str:
        value = args.get(key)
        if value is None and not required:
            return ""
        if not isinstance(value, str):
            raise ToolError(f"'{key}' must be text.")
        return value

    def _files(self, base: Path):
        if base.is_file():
            yield base
            return
        for path in sorted(base.rglob("*")):
            if any(part in SKIP_DIRS for part in path.relative_to(self.root).parts):
                continue
            if path.is_file():
                yield path

    # --- entry point ----------------------------------------------------------

    def run(self, name: str, args: dict) -> str:
        if not isinstance(args, dict):
            raise ToolError("Tool input must be an object.")
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            raise ToolError(f"There is no tool called {name}.")
        return handler(args)

    # --- tools ----------------------------------------------------------------

    def _tool_read_file(self, args: dict) -> str:
        path = self._path(args.get("path"))
        if not path.is_file():
            raise ToolError(f"{self._rel(path)} does not exist or is not a file.")
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError as exc:
            raise ToolError(f"Could not read {self._rel(path)}: {exc}")
        start = max(int(args.get("offset") or 1), 1)
        count = min(int(args.get("limit") or MAX_READ_LINES), MAX_READ_LINES)
        chunk = lines[start - 1:start - 1 + count]
        body = "\n".join(f"{n}\t{line}" for n, line in enumerate(chunk, start))
        more = len(lines) - (start - 1 + len(chunk))
        if more > 0:
            body += f"\n… {more} more lines (use offset {start + len(chunk)})"
        return _truncate(body or "(empty file)")

    def _tool_write_file(self, args: dict) -> str:
        path = self._path(args.get("path"))
        content = self._text(args, "content")
        rel = self._rel(path)
        verb = "Overwrite" if path.exists() else "Create"
        preview = content if len(content) < 1500 else content[:1500] + "\n…"
        if not self.approve(f"{verb} {rel} ({len(content.splitlines())} lines)", preview):
            raise ToolError(f"The user did not allow writing {rel}.")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return f"Wrote {rel} ({len(content.splitlines())} lines)."

    def _tool_edit_file(self, args: dict) -> str:
        path = self._path(args.get("path"))
        old, new = self._text(args, "old_text"), self._text(args, "new_text")
        rel = self._rel(path)
        if not path.is_file():
            raise ToolError(f"{rel} does not exist.")
        text = path.read_text(errors="replace")
        count = text.count(old) if old else 0
        if count == 0:
            raise ToolError(f"old_text was not found in {rel}. Read the file and copy the text exactly.")
        if count > 1:
            raise ToolError(f"old_text appears {count} times in {rel}. Include more surrounding lines.")
        updated = text.replace(old, new, 1)
        diff = "".join(difflib.unified_diff(text.splitlines(True), updated.splitlines(True),
                                            f"a/{rel}", f"b/{rel}", n=2))
        if not self.approve(f"Edit {rel}", diff):
            raise ToolError(f"The user did not allow editing {rel}.")
        path.write_text(updated)
        return f"Edited {rel}."

    def _tool_list_files(self, args: dict) -> str:
        pattern = self._text(args, "pattern", required=False) or "**/*"
        found = []
        for path in sorted(self.root.glob(pattern)):
            rel_parts = path.relative_to(self.root).parts
            if path.is_file() and not any(part in SKIP_DIRS for part in rel_parts):
                found.append(self._rel(path))
                if len(found) >= MAX_LIST:
                    found.append(f"… (stopped at {MAX_LIST} files)")
                    break
        return "\n".join(found) or "No files match."

    def _tool_search(self, args: dict) -> str:
        try:
            regex = re.compile(self._text(args, "pattern"))
        except re.error as exc:
            raise ToolError(f"Not a valid regular expression: {exc}")
        base = self._path(args.get("path") or ".")
        hits = []
        for path in self._files(base):
            try:
                if path.stat().st_size > 2_000_000:
                    continue
                for n, line in enumerate(path.read_text(errors="strict").splitlines(), 1):
                    if regex.search(line):
                        hits.append(f"{self._rel(path)}:{n}: {line.strip()[:200]}")
                        if len(hits) >= MAX_MATCHES:
                            return "\n".join(hits) + f"\n… (stopped at {MAX_MATCHES} matches)"
            except (UnicodeDecodeError, OSError):
                continue  # binary or unreadable file
        return "\n".join(hits) or "No matches."

    def _tool_run_command(self, args: dict) -> str:
        command = self._text(args, "command").strip()
        if not command:
            raise ToolError("The command is empty.")
        if not self.approve("Run a command", command):
            raise ToolError("The user did not allow this command.")
        shell = ["powershell", "-NoProfile", "-Command", command] if sys.platform == "win32" else ["bash", "-lc", command]
        try:
            done = subprocess.run(shell, cwd=self.root, capture_output=True, text=True,
                                  timeout=COMMAND_TIMEOUT_SEC, errors="replace")
        except subprocess.TimeoutExpired:
            raise ToolError(f"The command took longer than {COMMAND_TIMEOUT_SEC} seconds and was stopped.")
        output = (done.stdout + ("\n" + done.stderr if done.stderr else "")).strip()
        return _truncate(f"{output}\n(exit code {done.returncode})" if output else f"(no output, exit code {done.returncode})")
