"""⚡ Volt Code: a coding assistant in the terminal, part of Volt AI.

    volt                      chat about the project in the current folder
    volt "fix the tests"      do one task, then exit
    volt --brain local        use the free local model even when a Claude key is saved

It reads, searches, edits and creates files in the current folder and can run
commands. Writing files and running commands always ask first.
"""

from __future__ import annotations

import argparse
import os
import platform
import sys
import time
from pathlib import Path

from volt_code.agent import run_turn
from volt_code.brains import BrainError, ClaudeBrain, LocalBrain
from volt_code.tools import Toolbox

# --- Colours (off when not a terminal or NO_COLOR is set) ------------------------------

if sys.platform == "win32":
    os.system("")  # switch the Windows console into ANSI mode
COLOUR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def paint(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOUR else text


ACCENT = lambda t: paint("38;5;209", t)  # noqa: E731  Volt orange
# readline needs colour codes in the prompt wrapped in \001…\002 to measure its width.
PROMPT = "\001\033[38;5;209m\002› \001\033[0m\002" if COLOUR else "› "
DIM = lambda t: paint("2", t)  # noqa: E731
GREEN = lambda t: paint("32", t)  # noqa: E731
RED = lambda t: paint("31", t)  # noqa: E731
BOLD = lambda t: paint("1", t)  # noqa: E731

SYSTEM_PROMPT = """You are Volt Code, the coding assistant of Volt AI, working in the user's terminal.
Project folder: {root}
Operating system: {os}
Today: {today}

How to work:
- Use the tools to look at the project before answering questions about it or changing it.
- Read a file before editing it. Make focused changes with edit_file; use write_file for new files.
- After changing code, run the project's tests or a quick check with run_command when that makes sense.
- Keep replies short and plain: say what you did and anything the user needs to know.
- If a request is unclear or risky (deleting data, force pushing), ask first instead of guessing."""

HELP = f"""
  {BOLD('Commands')}
    /help          show this help
    /clear         start a fresh conversation
    /brain local   use the free local model     /brain claude   use Claude (needs your key)
    /exit          quit (or press Ctrl+D)
  Press Ctrl+C to stop Volt while it's working.
"""


class TerminalUI:
    def __init__(self):
        self.writing = False
        self.always: set[str] = set()

    def text_chunk(self, text: str) -> None:
        if not self.writing:
            print(ACCENT("⏺ "), end="")
            self.writing = True
        print(text, end="", flush=True)

    def end_text(self) -> None:
        if self.writing:
            print()
            self.writing = False

    def tool_start(self, call) -> None:
        detail = call.input.get("path") or call.input.get("pattern") or call.input.get("command") or ""
        print(DIM(f"  🔧 {call.name} ") + str(detail)[:120])

    def tool_done(self, call, output: str, failed: bool) -> None:
        lines = output.strip().splitlines() or [""]
        first = lines[0][:140]
        more = f" (+{len(lines) - 1} lines)" if len(lines) > 1 else ""
        print(("     " + RED("✗ " + first)) if failed else DIM(f"     ↳ {first}{more}"))

    def notice(self, message: str) -> None:
        print(ACCENT("  ⚠️  " + message))

    def approve(self, title: str, detail: str) -> bool:
        kind = "commands" if title.startswith("Run") else "file changes"
        if kind in self.always:
            return True
        self.end_text()
        print(BOLD(f"\n  ✋ {title}"))
        for line in detail.rstrip().splitlines()[:60]:
            if line.startswith("+") and not line.startswith("+++"):
                line = GREEN(line)
            elif line.startswith("-") and not line.startswith("---"):
                line = RED(line)
            print("   │ " + line)
        while True:
            try:
                answer = input(f"  Allow? {BOLD('y')}es / {BOLD('a')}lways allow {kind} / {BOLD('n')}o: ").strip().lower()
            except EOFError:
                return False
            if answer in ("y", "yes"):
                return True
            if answer in ("a", "always"):
                self.always.add(kind)
                return True
            if answer in ("n", "no", ""):
                return False


def make_brain(choice: str, root: Path, model: str | None):
    from desktop_app import volt_claude, volt_fast

    system = SYSTEM_PROMPT.format(root=root, os=f"{platform.system()} {platform.release()}",
                                  today=time.strftime("%A %d %B %Y"))
    key = volt_claude.load_key()
    if choice == "claude" or (choice == "auto" and key):
        if not key:
            raise BrainError("No Claude API key is saved. Add one in Volt AI's Settings, or use /brain local.")
        model = model if model in volt_claude.MODELS else volt_claude.DEFAULT_MODEL
        return ClaudeBrain(key, model, system), volt_claude.MODELS[model]
    local_model, url = volt_fast.model_and_url()
    return LocalBrain(model or local_model, url, system), f"{model or local_model} (free, on this computer)"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="volt", description="⚡ Volt Code: a coding assistant in your terminal.")
    parser.add_argument("task", nargs="*", help="do this one task, then exit")
    parser.add_argument("--brain", choices=("auto", "claude", "local"), default="auto",
                        help="auto (default) uses Claude when a key is saved, otherwise the local model")
    parser.add_argument("--model", help="model to use (for example claude-sonnet-5-5 or qwen2.5:3b)")
    args = parser.parse_args(argv)

    root = Path.cwd().resolve()
    ui = TerminalUI()
    toolbox = Toolbox(root, approve=ui.approve)
    try:
        brain, brain_name = make_brain(args.brain, root, args.model)
    except BrainError as exc:
        print(RED(f"❌ {exc}"))
        return 1

    def ask(text: str) -> None:
        try:
            run_turn(brain, toolbox, text, ui)
        except KeyboardInterrupt:
            ui.end_text()
            print(ACCENT("\n  ⏹  Stopped."))
        except BrainError as exc:
            ui.end_text()
            print(RED(f"  ❌ {exc}"))

    if args.task:
        ask(" ".join(args.task))
        return 0

    print(f"\n {ACCENT('⚡ Volt Code')}  {DIM('·')} {brain_name}")
    print(DIM(f"   📁 {root}"))
    print(DIM("   Type what you want to build or fix. /help for commands, Ctrl+D to quit.\n"))
    try:
        import readline  # noqa: F401  arrow keys and history

        history = Path.home() / ".local" / "state" / "volt-ai" / "volt-code-history"
        history.parent.mkdir(parents=True, exist_ok=True)
        try:
            readline.read_history_file(history)
        except OSError:
            pass
        import atexit

        atexit.register(lambda: readline.write_history_file(history))
    except ImportError:
        pass  # Windows without readline: plain input still works

    while True:
        try:
            text = input(PROMPT).strip()
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print()
            continue
        if not text:
            continue
        if text in ("/exit", "/quit", "exit", "quit"):
            return 0
        if text == "/help":
            print(HELP)
            continue
        if text == "/clear" or text.startswith("/brain"):
            choice = text.split()[1] if text.startswith("/brain") and len(text.split()) > 1 else args.brain
            try:
                brain, brain_name = make_brain(choice, root, args.model if choice == args.brain else None)
                print(DIM(f"  ✨ New conversation · {brain_name}"))
            except BrainError as exc:
                print(RED(f"  ❌ {exc}"))
            continue
        ask(text)


if __name__ == "__main__":
    sys.exit(main())
