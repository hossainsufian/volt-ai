"""Volt Code's agent loop: let the brain work with the tools until it's done."""

from __future__ import annotations

from volt_code.computer import FAILED_EARLIER, ComputerError
from volt_code.tools import Toolbox, ToolError

MAX_STEPS = 40


def run_turn(brain, toolbox: Toolbox, text: str, ui, max_steps: int = MAX_STEPS, computer=None) -> None:
    """Handle one user request. Ctrl+C (KeyboardInterrupt) stops it cleanly:
    every tool call the brain made still gets an answer, so the conversation
    stays valid for the next request."""
    brain.add_user(text)
    for _ in range(max_steps):
        getattr(ui, "thinking", lambda: None)()  # e.g. show a "Thinking…" spinner
        calls = brain.step(ui.text_chunk)
        ui.end_text()
        if not calls:
            return
        results = []
        computer_failed = False
        try:
            for call in calls:
                if call.toolset and computer_failed:
                    results.append((call, FAILED_EARLIER, True))
                    continue
                if call.error:
                    results.append((call, call.error, True))
                    ui.tool_done(call, call.error, True)
                    continue
                ui.tool_start(call)
                try:
                    if call.toolset:
                        output, failed = run_computer(call, computer, toolbox), False
                    else:
                        output, failed = toolbox.run(call.name, call.input), False
                except (ToolError, ComputerError) as exc:
                    output, failed = str(exc), True
                    computer_failed = computer_failed or bool(call.toolset)
                results.append((call, output, failed))
                ui.tool_done(call, output, failed)
        except KeyboardInterrupt:
            answered = {call.id for call, _, _ in results}
            results += [(call, "Stopped by the user.", True) for call in calls if call.id not in answered]
            brain.add_results(results)
            raise
        brain.add_results(results)
    ui.notice(f"Stopped after {max_steps} steps. Say 'continue' to keep going.")


def run_computer(call, computer, toolbox: Toolbox):
    """One screen, mouse or keyboard action, after the user has allowed screen control."""
    if computer is None:
        raise ComputerError("Screen control is off. The user can turn it on with /computer.")
    detail = ", ".join(f"{k}={v}" for k, v in (call.input or {}).items()) or "look at the screen"
    if not toolbox.approve(f"Control the screen: {call.name}", detail):
        raise ComputerError("The user did not allow controlling the screen.")
    return computer.run(call.name, call.input)
