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

from volt_code import features, sessions
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
PROMPT = "\001\033[1m\002> \001\033[0m\002" if COLOUR else "> "
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
  {BOLD('Volt Code commands')}
    /help              show this help
    /init              look at the project and write VOLT.md (instructions Volt reads every time)
    /memory            show VOLT.md        # some note     add a line to VOLT.md
    /resume            pick an earlier chat from this folder (also: volt --resume, volt -c)
    /compact           summarise the chat so far to free up room
    /clear             start a fresh conversation
    /model [name]      switch brain: local, gemini or claude
    /permissions       switch mode: ask · accept edits · plan (read-only)
    /status            brain, folder, mode and memory
    /computer          let Volt see the screen and use mouse and keyboard (needs Claude)
    /exit              quit (or Ctrl+D)
  {BOLD('In your message')}
    @path/to/file      attach a file           !command    run a shell command yourself
    line ending in \\   continue on the next line
  Ctrl+C stops Volt while it's working.
"""

SPINNER = "✻✶✳✢·✢✳✶"


class TerminalUI:
    """Claude Code-style output: ⏺ Tool(args) with ⎿ results, a Thinking… spinner, and
    numbered permission questions."""

    def __init__(self, mode=lambda: "default"):
        self.writing = False
        self.always: set[str] = set()
        self.mode = mode
        self._spin = None

    # --- spinner -------------------------------------------------------------------
    def thinking(self) -> None:
        import threading

        self._stop_spin()
        if not sys.stdout.isatty():
            return
        stop = threading.Event()

        def spin():
            start, i = time.time(), 0
            while not stop.wait(0.12):
                secs = int(time.time() - start)
                sys.stdout.write(f"\r{ACCENT(SPINNER[i % len(SPINNER)])} {ACCENT('Thinking…')} "
                                 f"{DIM(f'({secs}s · Ctrl+C to stop)')}\x1b[K")
                sys.stdout.flush()
                i += 1
            sys.stdout.write("\r\x1b[K")
            sys.stdout.flush()

        thread = threading.Thread(target=spin, daemon=True)
        self._spin = (stop, thread)
        thread.start()

    def _stop_spin(self) -> None:
        if self._spin:
            stop, thread = self._spin
            stop.set()
            thread.join(timeout=1)
            self._spin = None

    # --- text and tools ------------------------------------------------------------
    def text_chunk(self, text: str) -> None:
        self._stop_spin()
        if not self.writing:
            print("\n" + BOLD("⏺ "), end="")
            self.writing = True
        print(text, end="", flush=True)

    def end_text(self) -> None:
        self._stop_spin()
        if self.writing:
            print()
            self.writing = False

    def tool_start(self, call) -> None:
        self._stop_spin()
        print("\n" + GREEN("⏺ ") + BOLD(features.tool_label(call)))

    def tool_done(self, call, output, failed: bool) -> None:
        self._stop_spin()
        print(("  ⎿  " + RED(self._summary(call, output))) if failed else DIM("  ⎿  " + self._summary(call, output)))

    @staticmethod
    def _summary(call, output) -> str:
        if isinstance(output, list):  # computer action: image and/or text blocks
            if any(b.get("type") == "image" for b in output):
                return "📸 screenshot"
            return " ".join(b.get("text", "") for b in output)[:160]
        text = str(output).strip()
        lines = text.splitlines() or [""]
        if call.name == "read_file":
            return f"Read {len([l for l in lines if l[:1].isdigit()])} lines"
        if call.name == "list_files":
            return "No files" if text.startswith("No files") else f"Found {len(lines)} files"
        if call.name == "search":
            return "No matches" if text.startswith("No matches") else f"Found {len(lines)} matches"
        if call.name == "run_command":
            shown = "\n     ".join(l[:160] for l in lines[:6])
            return shown + (f"\n     … +{len(lines) - 6} lines" if len(lines) > 6 else "")
        return lines[0][:160]

    def notice(self, message: str) -> None:
        self._stop_spin()
        print(ACCENT("  ⚠️  " + message))

    # --- permissions ------------------------------------------------------------------
    def approve(self, title: str, detail: str) -> bool:
        self._stop_spin()
        decided = features.mode_decides(self.mode(), title)
        if decided is not None:
            if decided is False:
                print(DIM(f"  ⎿  ⏸ Not done in plan mode: {title}"))
            return decided
        kind = ("commands" if title.startswith("Run") else
                "screen control" if title.startswith("Control the screen") else "file changes")
        if kind in self.always:
            return True
        self.end_text()
        print("\n" + BOLD(f"╭─ {title}"))
        for line in detail.rstrip().splitlines()[:60]:
            if line.startswith("+") and not line.startswith("+++"):
                line = GREEN(line)
            elif line.startswith("-") and not line.startswith("---"):
                line = RED(line)
            print("│ " + line)
        from volt_code.picker import choose

        picked = choose(BOLD("╰─ Do you want to proceed?"), "",
                        ["1. Yes", f"2. Yes, and don't ask again for {kind} this session",
                         "3. No, and tell Volt what to do differently (esc)"], paint=paint,
                        action="select", searchable=False)
        if picked == 1:
            self.always.add(kind)
        return picked in (0, 1)


def make_brain(choice: str, root: Path, model: str | None, computer: bool = False):
    from desktop_app import volt_claude, volt_fast, volt_online

    system = SYSTEM_PROMPT.format(root=root, os=f"{platform.system()} {platform.release()}",
                                  today=time.strftime("%A %d %B %Y"))
    memory = features.load_project_memory(root)
    if memory:
        system += f"\n\nProject instructions from {features.MEMORY_FILE} (written by the user; follow them):\n{memory}"
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
    secs = max(0, int(time.time() - ts))
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    if secs < 86400 * 30:
        return f"{secs // 86400}d ago"
    return time.strftime("%d %b", time.localtime(ts))


def pick_chat(root: Path):
    """Claude Code-style resume list: arrow keys, type to search, Enter to resume."""
    from volt_code.picker import choose

    chats = sessions.list_for(root)[:200]
    if not chats:
        print(DIM("  No saved chats in this folder yet."))
        return None
    rows = [f"{_age(c.updated):>11}  {_age(c.created):>11}  {c.message_count:>10}  {c.title[:70]}" for c in chats]
    header = f"  {'Modified':>11}  {'Created':>11}  {'# Messages':>10}  Summary"
    picked = choose(BOLD("\n  Resume a conversation"), header, rows,
                    search=[c.title for c in chats], paint=paint)
    return chats[picked] if picked is not None else None


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
    state = {"computer": None, "mode": "default", "context": ""}
    ui = TerminalUI(mode=lambda: state["mode"])
    toolbox = Toolbox(root, approve=ui.approve)

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
        # Show the earlier conversation, like Claude Code does when it resumes.
        print(DIM(f"\n  ↩️  Resumed: {chat.title}  ({state['name']})\n"))
        history = chat.recap(limit=10_000)
        if len(history) > 60:
            print(DIM(f"  … {len(history) - 60} earlier messages …\n"))
        for who, text in history[-60:]:
            body = text if len(text) <= 1200 else text[:1200] + " …"
            if who == "You":
                print(ACCENT("› ") + body.replace("\n", "\n  "))
            else:
                print(ACCENT("⏺ ") + body.replace("\n", "\n  "))
            print()
        return True

    if args.computer and not computer_on():
        return 1
    if not start(args.brain, args.model):
        return 1

    def ask(text: str) -> None:
        text, attached = features.expand_mentions(text, root)
        for rel in attached:
            print(DIM(f"  ⎿  Attached {rel}"))
        if state["context"]:  # output of !commands since the last message
            text = state["context"] + "\n\n" + text
            state["context"] = ""
        if state["mode"] == "plan":
            text += features.PLAN_PROMPT
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

    welcome(root, state["name"])
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

    def compact() -> None:
        collected: list[str] = []
        brain = state["brain"]
        ui.thinking()
        try:
            brain.add_user(features.COMPACT_REQUEST)
            calls = brain.step(collected.append)
            if calls:
                brain.add_results([(c, "Not now: only summarise.", True) for c in calls])
        except (BrainError, KeyboardInterrupt) as exc:
            ui.end_text()
            print(RED(f"  ❌ Couldn't compact: {exc}"))
            return
        ui.end_text()
        summary = "".join(collected).strip()
        if not summary:
            print(RED("  ❌ The summary came back empty; the chat was left as it was."))
            return
        label = state["brain"].label
        if start(BRAIN_CHOICE.get(label, "local"), args.model):
            state["brain"].add_user(f"Summary of our conversation so far:\n{summary}")
            state["brain"].messages.append({"role": "assistant", "content": "Thanks, I have the context. What next?"})
            print(DIM("  ✻ Conversation compacted. ") + DIM("(the summary keeps the important parts)"))

    while True:
        mode_text = features.MODE_TEXT.get(state["mode"], "")
        if mode_text:
            print(ACCENT(f"  {mode_text}") + DIM("  (/permissions to change)"))
        try:
            text = input(PROMPT).strip()
            while text.endswith("\\"):  # a trailing backslash continues on the next line
                text = text[:-1] + "\n" + input("  ").strip()
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
        if text.startswith("!"):
            command = text[1:].strip()
            if command:
                output = features.run_shell(command, root)
                print(GREEN("⏺ ") + BOLD(f"Bash({command[:100]})"))
                for n, line in enumerate(output.splitlines()[:40]):
                    print(DIM(("  ⎿  " if n == 0 else "     ") + line[:200]))
                state["context"] += f"I ran this in the terminal:\n$ {command}\n```\n{output[-8000:]}\n```\n"
            continue
        if text.startswith("#") and not text.startswith("#!"):
            path = features.remember(root, text.lstrip("#").strip())
            print(DIM(f"  ⎿  Saved to {path.name} (Volt reads it in new conversations)"))
            continue
        if text == "/init":
            ask(features.INIT_REQUEST)
            if features.memory_path(root).exists():
                start(BRAIN_CHOICE.get(state["brain"].label, args.brain), args.model)
                print(DIM(f"  ⎿  {features.MEMORY_FILE} is ready and loaded into this new conversation."))
            continue
        if text == "/memory":
            memory = features.load_project_memory(root)
            if memory:
                print(DIM(f"  ⎿  {features.memory_path(root)}"))
                for line in memory.splitlines()[:40]:
                    print("     " + line)
            else:
                print(DIM(f"  ⎿  No {features.MEMORY_FILE} yet. Run /init, or type '# a note' to start one."))
            continue
        if text == "/status":
            memory = features.load_project_memory(root)
            print(DIM(f"  ⎿  Brain: {state['name']}\n     Folder: {root}\n     Mode: "
                      f"{features.MODE_TEXT.get(state['mode']) or 'ask before changes'}\n     "
                      f"{features.MEMORY_FILE}: {'loaded' if memory else 'none'}\n     "
                      f"Screen control: {'on' if state['computer'] else 'off'}\n     Chat: {state['session'].id}"))
            continue
        if text.startswith("/permissions"):
            want = text.split()[1] if len(text.split()) > 1 else ""
            aliases = {"ask": "default", "default": "default", "edits": "accept_edits",
                       "accept": "accept_edits", "plan": "plan"}
            state["mode"] = aliases.get(want, features.next_mode(state["mode"]))
            print(DIM("  ⎿  Mode: ") + ACCENT(features.MODE_TEXT.get(state["mode"]) or "ask before every change"))
            continue
        if text == "/compact":
            compact()
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
        if text in ("/model", "/brain"):
            print(DIM(f"  ⎿  Now: {state['name']}\n     Switch with /model local · /model gemini · /model claude"))
            continue
        if text == "/clear" or text.startswith("/brain ") or text.startswith("/model "):
            choice = text.split()[1] if " " in text else args.brain
            if start(choice, args.model if choice == args.brain else None):
                print(DIM(f"  ⎿  New conversation · {state['name']}"))
            continue
        if text.startswith("/"):
            print(DIM("  ⎿  Unknown command. /help lists them."))
            continue
        ask(text)


def welcome(root: Path, brain_name: str) -> None:
    """Claude Code-style welcome box."""
    width = 58
    inner = [ACCENT("✻") + " " + BOLD("Welcome to Volt Code!"), "",
             DIM("  /help for help, /status for your current setup"), "",
             DIM(f"  cwd: {str(root)[-(width - 9):]}"), DIM(f"  brain: {brain_name[:width - 11]}")]
    visible = ["✻ Welcome to Volt Code!", "", "  /help for help, /status for your current setup", "",
               f"  cwd: {str(root)[-(width - 9):]}", f"  brain: {brain_name[:width - 11]}"]
    print("\n" + ACCENT("╭" + "─" * width + "╮"))
    for shown, plain in zip(inner, visible):
        print(ACCENT("│") + " " + shown + " " * max(0, width - 1 - len(plain)) + ACCENT("│"))
    print(ACCENT("╰" + "─" * width + "╯"))
    tips = [] if features.load_project_memory(root) else [
        "Run /init to create a VOLT.md file with instructions for Volt"]
    tips += ["Ask Volt to explain, fix or build something", "Use @file to attach a file, !command to run one yourself",
             "Be as specific as you would with another engineer"]
    print(DIM("\n Tips for getting started:"))
    for n, tip in enumerate(tips, 1):
        print(DIM(f"  {n}. {tip}"))
    print()


if __name__ == "__main__":
    sys.exit(main())
