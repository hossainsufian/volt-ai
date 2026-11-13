"""⚡ Volt Code: a coding assistant in the terminal, part of Volt AI.

    volt                      chat about the project in the current folder
    volt "fix the tests"      do one task, then exit
    volt --brain local        use the free local model even when a Claude key is saved
    volt -c                   continue the last chat in this folder
    volt --resume             pick an earlier chat from this folder to continue

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

from volt_code import sessions
from volt_code.agent import run_turn
from volt_code.brains import BrainError, ClaudeBrain, LocalBrain, OnlineBrain
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
    /brain local   free model on this computer
    /brain gemini  fast online brain (needs your Gemini key)
    /brain claude  strongest for coding (needs your Claude key)
    /resume        continue an earlier chat from this folder
    /computer      let Volt see the screen and use mouse and keyboard (needs Claude)
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
        args = call.input if isinstance(call.input, dict) else {}
        if call.toolset:
            detail = args.get("text") or args.get("coordinate") or args.get("region") or ""
            print(DIM(f"  🖱️  {call.name} ") + str(detail)[:120])
            return
        detail = args.get("path") or args.get("pattern") or args.get("command") or ""
        print(DIM(f"  🔧 {call.name} ") + str(detail)[:120])

    def tool_done(self, call, output, failed: bool) -> None:
        if isinstance(output, list):  # computer action: image and/or text blocks
            if any(b.get("type") == "image" for b in output):
                print(DIM("     ↳ 📸 screenshot"))
            else:
                print(DIM("     ↳ " + " ".join(b.get("text", "") for b in output)[:140]))
            return
        lines = str(output).strip().splitlines() or [""]
        first = lines[0][:140]
        more = f" (+{len(lines) - 1} lines)" if len(lines) > 1 else ""
        print(("     " + RED("✗ " + first)) if failed else DIM(f"     ↳ {first}{more}"))

    def notice(self, message: str) -> None:
        print(ACCENT("  ⚠️  " + message))

    def approve(self, title: str, detail: str) -> bool:
        kind = ("commands" if title.startswith("Run") else
                "screen control" if title.startswith("Control the screen") else "file changes")
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


def make_brain(choice: str, root: Path, model: str | None, computer: bool = False):
    from desktop_app import volt_claude, volt_fast, volt_online

    system = SYSTEM_PROMPT.format(root=root, os=f"{platform.system()} {platform.release()}",
                                  today=time.strftime("%A %d %B %Y"))
    key = volt_claude.load_key()
    if choice == "claude" or (choice == "auto" and key):
        if not key:
            raise BrainError("No Claude API key is saved. Add one in Volt AI's Settings, or use /brain local.")
        model = model if model in volt_claude.MODELS else volt_claude.DEFAULT_MODEL
        return (ClaudeBrain(key, model, system, computer=computer),
                volt_claude.MODELS[model] + (" · 🖥️ screen control" if computer else ""))
    if computer:
        raise BrainError("Screen control needs Claude (it has to see the screen). Add a Claude API key in Volt AI's Settings.")
    online = volt_online.load()
    if choice in ("gemini", "online") or (choice == "auto" and online):
        if not online:
            raise BrainError("No Gemini API key is saved. Add one in Volt AI's Settings, or use /brain local.")
        name = model or online.get("model") or volt_online.FALLBACK_MODEL
        return (OnlineBrain(online["key"], name, online.get("base_url") or volt_online.BASE_URL, system),
                f"{volt_online.PROVIDER} · {name} (fast, online)")
    local_model, url = volt_fast.model_and_url()
    return LocalBrain(model or local_model, url, system), f"{model or local_model} (free, on this computer)"


BRAIN_CHOICE = {"Claude": "claude", "Online": "gemini", "Local": "local"}


def _age(ts: float) -> str:
    mins = int((time.time() - ts) / 60)
    if mins < 60:
        return f"{max(mins, 1)} min ago"
    if mins < 60 * 24:
        return f"{mins // 60} h ago"
    return time.strftime("%d %b", time.localtime(ts))


def pick_chat(root: Path):
    """Ask which saved chat from this folder to continue. None when there are none or none picked."""
    chats = sessions.list_for(root)[:15]
    if not chats:
        print(DIM("  No saved chats in this folder yet."))
        return None
    print(BOLD("\n  Earlier chats in this folder"))
    for n, chat in enumerate(chats, 1):
        print(f"   {ACCENT(str(n).rjust(2))}  {chat.title[:60]}  {DIM(f'· {chat.brain} · {_age(chat.updated)}')}")
    try:
        answer = input("  Number to continue (Enter to cancel): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if answer.isdigit() and 1 <= int(answer) <= len(chats):
        return chats[int(answer) - 1]
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="volt", description="⚡ Volt Code: a coding assistant in your terminal.")
    parser.add_argument("task", nargs="*", help="do this one task, then exit")
    parser.add_argument("--brain", choices=("auto", "claude", "gemini", "local"), default="auto",
                        help="auto (default) uses Claude or Gemini when a key is saved, otherwise the local model")
    parser.add_argument("--model", help="model to use (for example claude-sonnet-5-5 or qwen2.5:3b)")
    parser.add_argument("-c", "--continue", dest="cont", action="store_true",
                        help="continue the last chat in this folder")
    parser.add_argument("-r", "--resume", action="store_true", help="pick an earlier chat in this folder to continue")
    parser.add_argument("--computer", action="store_true",
                        help="let Volt see the screen and use the mouse and keyboard (needs Claude)")
    args = parser.parse_args(argv)

    root = Path.cwd().resolve()
    ui = TerminalUI()
    toolbox = Toolbox(root, approve=ui.approve)
    state = {"computer": None}

    def computer_on() -> bool:
        from volt_code.computer import Computer, unsupported_reason

        reason = unsupported_reason()
        if reason:
            print(RED(f"  ❌ {reason}"))
            return False
        try:
            state["computer"] = Computer()
        except Exception as exc:  # no screen access in this session
            print(RED(f"  ❌ Can't reach the screen here ({type(exc).__name__})."))
            return False
        return True

    def start(choice: str, model) -> bool:
        try:
            state["brain"], state["name"] = make_brain(choice, root, model,
                                                       computer=state["computer"] is not None)
        except BrainError as exc:
            print(RED(f"❌ {exc}"))
            return False
        state["session"] = sessions.Session.new(root, state["brain"].label)
        return True

    def resume(chat) -> bool:
        if chat.brain != state["brain"].label and not start(BRAIN_CHOICE.get(chat.brain, "local"), None):
            return False
        if not chat.restore_into(state["brain"]):
            print(RED("  ❌ That chat can't be continued with this brain."))
            return False
        state["session"] = chat
        print(DIM(f"  ↩️  Continuing: {chat.title}  ({state['name']})"))
        for who, text in chat.recap():
            line = text.replace("\n", " ")
            print(DIM(f"   {who}: ") + (line[:160] + ("…" if len(line) > 160 else "")))
        print()
        return True

    if args.computer and not computer_on():
        return 1
    if not start(args.brain, args.model):
        return 1

    def ask(text: str) -> None:
        try:
            run_turn(state["brain"], toolbox, text, ui, computer=state["computer"],
                     max_steps=120 if state["computer"] else 40)
        except KeyboardInterrupt:
            ui.end_text()
            print(ACCENT("\n  ⏹  Stopped."))
        except BrainError as exc:
            ui.end_text()
            print(RED(f"  ❌ {exc}"))
        finally:
            try:
                state["session"].save(state["brain"])
            except OSError:
                pass  # saving is a convenience; never break the chat for it

    if args.cont or args.resume:
        chat = sessions.latest(root) if args.cont else pick_chat(root)
        if chat is None and args.cont:
            print(DIM("  No earlier chat in this folder, starting a new one."))
        elif chat is not None and not resume(chat):
            return 1

    if args.task:
        ask(" ".join(args.task))
        return 0

    print(f"\n {ACCENT('⚡ Volt Code')}  {DIM('·')} {state['name']}")
    print(DIM(f"   📁 {root}"))
    print(DIM("   Type what you want to build or fix. /help for commands, /resume for earlier chats.\n"))
    try:
        import readline  # noqa: F401  arrow keys and history

        history = sessions.sessions_dir().parent / "volt-code-history"
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
        if text == "/computer":
            if state["computer"] is None:
                if computer_on() and start("claude", args.model):
                    print(ACCENT("  🖥️  Screen control is ON. Volt can see the screen and use the mouse and keyboard."))
                    print(DIM("     It asks before its first screen action. Ctrl+C stops it any time. /computer turns it off."))
                else:
                    state["computer"] = None
            else:
                state["computer"] = None
                start(args.brain, args.model)
                print(DIM("  🖥️  Screen control is off (new conversation)."))
            continue
        if text in ("/resume", "/chats"):
            chat = pick_chat(root)
            if chat is not None:
                resume(chat)
            continue
        if text == "/clear" or text.startswith("/brain"):
            choice = text.split()[1] if text.startswith("/brain") and len(text.split()) > 1 else args.brain
            if start(choice, args.model if choice == args.brain else None):
                print(DIM(f"  ✨ New conversation · {state['name']}"))
            continue
        ask(text)


if __name__ == "__main__":
    sys.exit(main())
