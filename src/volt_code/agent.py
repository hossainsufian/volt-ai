"""Volt Code's agent loop: let the brain work with the tools until it's done."""

from __future__ import annotations

from volt_code.tools import Toolbox, ToolError

MAX_STEPS = 40


def run_turn(brain, toolbox: Toolbox, text: str, ui, max_steps: int = MAX_STEPS) -> None:
    """Handle one user request. Ctrl+C (KeyboardInterrupt) stops it cleanly:
    every tool call the brain made still gets an answer, so the conversation
    stays valid for the next request."""
    brain.add_user(text)
    for _ in range(max_steps):
        calls = brain.step(ui.text_chunk)
        ui.end_text()
        if not calls:
            return
        results = []
        try:
            for call in calls:
                if call.error:
                    results.append((call, call.error, True))
                    ui.tool_done(call, call.error, True)
                    continue
                ui.tool_start(call)
                try:
                    output, failed = toolbox.run(call.name, call.input), False
                except ToolError as exc:
                    output, failed = str(exc), True
                results.append((call, output, failed))
                ui.tool_done(call, output, failed)
        except KeyboardInterrupt:
            answered = {call.id for call, _, _ in results}
            results += [(call, "Stopped by the user.", True) for call in calls if call.id not in answered]
            brain.add_results(results)
            raise
        brain.add_results(results)
    ui.notice(f"Stopped after {max_steps} steps. Say 'continue' to keep going.")
