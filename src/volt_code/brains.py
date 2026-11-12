"""The two brains Volt Code can think with: Claude (your API key) or the free
local model through Ollama. Each keeps its own conversation in its API's
format and exposes the same three steps to the agent loop:

* ``add_user(text)`` - the user's next request
* ``step(on_text)`` - one model turn; streams text through ``on_text`` and
  returns the tool calls it wants (empty when the turn is finished)
* ``add_results(results)`` - the outcome of those tool calls
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from volt_code.tools import TOOLS

OnText = Callable[[str], None]


class BrainError(Exception):
    """The brain could not answer (no key, model missing, refused, offline)."""


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict
    error: Optional[str] = None  # set when the call can't be run as given


def check_input(name: str, args) -> Optional[str]:
    """Problems with a tool call's input, or None when it can run."""
    spec = next((t for t in TOOLS if t["name"] == name), None)
    if spec is None:
        return f"There is no tool called {name}."
    if not isinstance(args, dict):
        return "Tool input must be a JSON object."
    props = spec["parameters"]["properties"]
    for key in spec["parameters"]["required"]:
        if key not in args:
            return f"Missing '{key}'."
    for key, value in args.items():
        kind = props.get(key, {}).get("type")
        if kind == "string" and not isinstance(value, str):
            return f"'{key}' must be text."
        if kind == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
            return f"'{key}' must be a whole number."
    return None


# --- Claude ----------------------------------------------------------------------

class ClaudeBrain:
    label = "Claude"

    def __init__(self, api_key: str, model: str, system: str, client=None):
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key)
        self.client = client
        self.model = model
        self.system = system
        self.messages: list[dict] = []
        # Stream large tool inputs (file contents) as they're written; inputs
        # are then checked here before anything runs.
        self.tools = [{"name": t["name"], "description": t["description"],
                       "input_schema": t["parameters"], "eager_input_streaming": True} for t in TOOLS]

    def add_user(self, text: str) -> None:
        if self.messages and self.messages[-1]["role"] == "user":
            # The last turn was interrupted before Claude answered: keep one user turn.
            content = self.messages[-1]["content"]
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            self.messages[-1]["content"] = list(content) + [{"type": "text", "text": text}]
        else:
            self.messages.append({"role": "user", "content": text})

    def step(self, on_text: OnText) -> list[ToolCall]:
        import anthropic

        for attempt in range(3):
            try:
                with self.client.beta.messages.stream(
                    model=self.model,
                    max_tokens=64000,
                    system=self.system,
                    tools=self.tools,
                    messages=self.messages,
                    thinking={"type": "adaptive"},
                    output_config={"effort": "high"},
                    # If a safety filter declines, let Anthropic retry on a suitable model.
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                ) as stream:
                    for text in stream.text_stream:
                        on_text(text)
                    message = stream.get_final_message()
                break
            except ValueError:
                # A streamed tool input that isn't valid JSON: ask again.
                if attempt == 2:
                    raise BrainError("Claude sent a broken tool request three times. Try rephrasing.")
            except anthropic.AuthenticationError:
                raise BrainError("Your Claude API key was not accepted. Check it in Volt AI's Settings.")
            except anthropic.RateLimitError:
                raise BrainError("Claude is busy or your credit limit was reached. Wait a moment and try again.")
            except anthropic.APIConnectionError:
                raise BrainError("Can't reach Claude. Check the internet connection.")
            except anthropic.APIStatusError as exc:
                raise BrainError(f"Claude returned an error ({exc.status_code}).")

        self.messages.append({"role": "assistant", "content": message.content})
        calls = [ToolCall(b.id, b.name, b.input) for b in message.content if b.type == "tool_use"]
        if message.stop_reason == "refusal":
            for call in calls:
                call.error = "Not run: the reply was stopped by a safety filter."
            if not calls:
                raise BrainError("Claude declined this request.")
        elif message.stop_reason == "max_tokens":
            for call in calls:
                call.error = "Not run: the request was cut off. Split the change into smaller steps."
        else:
            for call in calls:
                call.error = check_input(call.name, call.input)
                if call.error:
                    call.error = "INVALID_INPUT: " + call.error
        return calls

    def add_results(self, results: list[tuple[ToolCall, str, bool]]) -> None:
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call.id, "content": output, "is_error": failed}
            for call, output, failed in results
        ]})


# --- Local model (Ollama) -----------------------------------------------------------

class LocalBrain:
    label = "Local"
    CONTEXT_TOKENS = 8192  # room for file contents; still fast on a small CPU

    def __init__(self, model: str, url: str, system: str, post=None):
        if post is None:
            import requests

            post = requests.post
        self.post = post
        self.model = model
        self.url = url.rstrip("/")
        self.messages: list[dict] = [{"role": "system", "content": system}]
        self.tools = [{"type": "function", "function": t} for t in TOOLS]

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def _text_tool_call(self, text: str) -> Optional[dict]:
        """Small models sometimes write a tool call as JSON text instead."""
        cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        if not (cleaned.startswith("{") and cleaned.endswith("}")):
            return None
        try:
            data = json.loads(cleaned)
        except ValueError:
            return None
        name = data.get("name")
        args = data.get("arguments", data.get("parameters"))
        if any(t["name"] == name for t in TOOLS) and isinstance(args, dict):
            return {"function": {"name": name, "arguments": args}}
        return None

    def step(self, on_text: OnText) -> list[ToolCall]:
        body = {"model": self.model, "messages": self.messages, "tools": self.tools, "stream": True,
                "keep_alive": "30m", "options": {"num_ctx": self.CONTEXT_TOKENS}}
        text, raw_calls = "", []
        try:
            with self.post(f"{self.url}/api/chat", json=body, stream=True, timeout=(10, 600)) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        raise BrainError(f"The local model failed: {chunk['error']}")
                    message = chunk.get("message") or {}
                    if message.get("content"):
                        text += message["content"]
                        on_text(message["content"])
                    raw_calls += message.get("tool_calls") or []
                    if chunk.get("done"):
                        break
        except BrainError:
            raise
        except Exception as exc:
            name = type(exc).__name__
            if "Connection" in name or "Timeout" in name:
                raise BrainError("The local model isn't running. Start Volt AI (or Ollama) and try again.")
            raise BrainError(f"The local model failed ({name}).")
        if not raw_calls:
            found = self._text_tool_call(text)
            if found:
                raw_calls = [found]
        self.messages.append({"role": "assistant", "content": text, "tool_calls": raw_calls} if raw_calls
                             else {"role": "assistant", "content": text})
        calls = []
        for raw in raw_calls:
            fn = raw.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    pass
            call = ToolCall(raw.get("id") or uuid.uuid4().hex[:12], fn.get("name", ""), args)
            call.error = check_input(call.name, args)
            calls.append(call)
        return calls

    def add_results(self, results: list[tuple[ToolCall, str, bool]]) -> None:
        for call, output, failed in results:
            self.messages.append({"role": "tool", "tool_name": call.name,
                                  "content": ("Error: " + output) if failed else output})


# --- Online brain (Gemini or any OpenAI-compatible service) --------------------------

class OnlineBrain:
    label = "Online"

    def __init__(self, api_key: str, model: str, base_url: str, system: str, post=None):
        if post is None:
            import requests

            post = requests.post
        self.post = post
        self.key = api_key
        self.model = model
        self.url = base_url.rstrip("/")
        self.messages: list[dict] = [{"role": "system", "content": system}]
        self.tools = [{"type": "function", "function": t} for t in TOOLS]

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def step(self, on_text: OnText) -> list[ToolCall]:
        body = {"model": self.model, "messages": list(self.messages), "tools": self.tools}
        try:
            resp = self.post(f"{self.url}/chat/completions", json=body,
                             headers={"Authorization": f"Bearer {self.key}"}, timeout=(10, 300))
            resp.raise_for_status()
            message = (resp.json().get("choices") or [{}])[0].get("message") or {}
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (400, 401, 403):
                raise BrainError("The online brain did not accept the request or your key. Check it in Volt AI's Settings.")
            if status == 429:
                raise BrainError("You reached the free limit for now. Wait a minute, or use /brain local.")
            name = type(exc).__name__
            if "Connection" in name or "Timeout" in name:
                raise BrainError("Can't reach the online brain. Check the internet connection.")
            raise BrainError(f"The online brain failed ({name}).")
        text = message.get("content") or ""
        raw_calls = message.get("tool_calls") or []
        if text:
            on_text(text)
        entry = {"role": "assistant", "content": text or None}
        if raw_calls:
            entry["tool_calls"] = raw_calls
        self.messages.append(entry)
        calls = []
        for raw in raw_calls:
            fn = raw.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except ValueError:
                    pass
            call = ToolCall(raw.get("id") or uuid.uuid4().hex[:12], fn.get("name", ""), args)
            call.error = check_input(call.name, args)
            calls.append(call)
        return calls

    def add_results(self, results: list[tuple[ToolCall, str, bool]]) -> None:
        for call, output, failed in results:
            self.messages.append({"role": "tool", "tool_call_id": call.id,
                                  "content": ("Error: " + output) if failed else output})
