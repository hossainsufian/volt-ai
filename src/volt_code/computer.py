"""Screen, mouse and keyboard control for Volt Code (Claude's computer toolset).

Claude sees the screen through screenshots and acts with the 17 members of the
`computer_toolset_20260801` tool: clicks, typing, keys, scrolling, dragging and
so on. Coordinates arrive in the pixel space of the (scaled) screenshots Volt
sends, so they are scaled back to the real screen here.

Works where Python can capture the screen and inject input: Windows, macOS and
Linux on X11. Wayland desktops block both for security.
"""

from __future__ import annotations

import base64
import io
import math
import os
import sys
import time

TOOLSET = "computer"
MAX_LONG_EDGE = 2576       # image limits for models that support the toolset
MAX_VISUAL_TOKENS = 4784   # ceil(w/28) * ceil(h/28)
SETTLE_SEC = 0.4           # let the screen update after an action
FAILED_EARLIER = "Not executed: an earlier computer action in this turn failed."


class ComputerError(Exception):
    """An action that could not be done; the message goes back to Claude."""


def unsupported_reason() -> str | None:
    """Why screen control can't work here, or None when it can."""
    if sys.platform.startswith("linux"):
        if os.environ.get("XDG_SESSION_TYPE") == "wayland" or (
                os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY")):
            return ("This desktop uses Wayland, which doesn't let apps take screenshots or move the "
                    "mouse. Screen control works on Windows, macOS and Linux X11 sessions.")
        if not os.environ.get("DISPLAY"):
            return "No desktop screen was found (DISPLAY is not set)."
    return None


def scale_for(width: int, height: int) -> float:
    """Factor (<= 1) that makes a screenshot fit the model's image limits."""
    scale = min(1.0, MAX_LONG_EDGE / max(width, height))
    while scale > 0.1 and math.ceil(width * scale / 28) * math.ceil(height * scale / 28) > MAX_VISUAL_TOKENS:
        scale *= 0.98
    return scale


# xdotool-style key names (what Claude sends) -> pynput key names.
KEY_ALIASES = {
    "return": "enter", "enter": "enter", "kp_enter": "enter", "escape": "esc", "esc": "esc",
    "backspace": "backspace", "back_space": "backspace", "tab": "tab", "space": "space",
    "delete": "delete", "del": "delete", "insert": "insert", "home": "home", "end": "end",
    "page_up": "page_up", "prior": "page_up", "pageup": "page_up",
    "page_down": "page_down", "next": "page_down", "pagedown": "page_down",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "ctrl": "ctrl", "control": "ctrl", "control_l": "ctrl_l", "control_r": "ctrl_r",
    "alt": "alt", "alt_l": "alt_l", "alt_r": "alt_r", "shift": "shift", "shift_l": "shift_l",
    "shift_r": "shift_r", "super": "cmd", "super_l": "cmd", "win": "cmd", "meta": "cmd", "cmd": "cmd",
    "caps_lock": "caps_lock", "capslock": "caps_lock", "print": "print_screen", "menu": "menu",
}


def parse_combo(text: str) -> list[str]:
    """'ctrl+shift+t' -> ['ctrl', 'shift', 't'] in pynput names (single characters stay as they are)."""
    if not isinstance(text, str) or not text.strip():
        raise ComputerError("A key name is required.")
    parts = [p for p in text.split("+") if p] if text != "+" else ["+"]
    out = []
    for part in parts:
        low = part.strip().lower()
        if low in KEY_ALIASES:
            out.append(KEY_ALIASES[low])
        elif len(low) > 1 and low[0] == "f" and low[1:].isdigit() and 1 <= int(low[1:]) <= 24:
            out.append(low)
        elif len(part) == 1:
            out.append(part)
        else:
            raise ComputerError(f"Unknown key '{part}'.")
    return out


class Computer:
    """Executes computer-toolset actions on this machine's screen."""

    def __init__(self, grab=None, mouse=None, keyboard=None):
        if grab is None:
            from PIL import ImageGrab

            grab = lambda bbox=None: ImageGrab.grab(bbox=bbox, all_screens=False)  # noqa: E731
        if mouse is None or keyboard is None:
            from pynput import keyboard as kb, mouse as ms

            mouse = mouse or ms.Controller()
            keyboard = keyboard or kb.Controller()
            self._Key, self._Button = kb.Key, ms.Button
        else:
            self._Key = self._Button = None
        self._grab, self.mouse, self.keyboard = grab, mouse, keyboard
        width, height = self._grab().size
        self.width, self.height = width, height
        self.scale = scale_for(width, height)

    # --- coordinates ---------------------------------------------------------------

    def _real(self, coordinate) -> tuple[int, int]:
        if not (isinstance(coordinate, (list, tuple)) and len(coordinate) == 2
                and all(isinstance(v, (int, float)) for v in coordinate)):
            raise ComputerError("coordinate must be [x, y].")
        x, y = coordinate[0] / self.scale, coordinate[1] / self.scale
        if not (0 <= x <= self.width and 0 <= y <= self.height):
            raise ComputerError(f"{list(coordinate)} is outside the screen.")
        return int(round(x)), int(round(y))

    # --- images --------------------------------------------------------------------

    def _image(self, image) -> list[dict]:
        fit = scale_for(*image.size)  # zoomed crops can still be large
        if fit < 1:
            image = image.resize((max(1, int(image.width * fit)), max(1, int(image.height * fit))))
        # JPEG keeps each screenshot ~150 KB (PNG of a photo wallpaper is ~1 MB), so long
        # tasks with many screenshots stay well under the request size limit.
        buf = io.BytesIO()
        image.convert("RGB").save(buf, format="JPEG", quality=80)
        data = base64.b64encode(buf.getvalue()).decode()
        return [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}]

    def screenshot(self) -> list[dict]:
        image = self._grab()
        if self.scale < 1:
            image = image.resize((int(self.width * self.scale), int(self.height * self.scale)))
        return self._image(image)

    # --- keys ----------------------------------------------------------------------

    def _key(self, name: str):
        if len(name) == 1 or self._Key is None:
            return name
        return getattr(self._Key, name)

    def _hold(self, modifiers):
        keys = [self._key(k) for k in parse_combo(modifiers)] if modifiers else []
        for k in keys:
            self.keyboard.press(k)
        return keys

    def _release(self, keys):
        for k in reversed(keys):
            self.keyboard.release(k)

    def _button(self, name: str):
        return getattr(self._Button, name) if self._Button is not None else name

    # --- entry point ---------------------------------------------------------------

    def run(self, name: str, args: dict) -> list[dict]:
        """Do one action; returns tool_result content blocks."""
        if not isinstance(args, dict):
            raise ComputerError("Input must be an object.")
        handler = getattr(self, f"_do_{name}", None)
        if handler is None:
            raise ComputerError(f"Unknown computer action {name}.")
        result = handler(args)
        if name not in ("screenshot", "zoom", "cursor_position", "wait"):
            time.sleep(SETTLE_SEC)
        return result if isinstance(result, list) else [{"type": "text", "text": result or "OK"}]

    def _do_screenshot(self, a):
        return self.screenshot()

    def _do_zoom(self, a):
        region = a.get("region")
        if not (isinstance(region, (list, tuple)) and len(region) == 4):
            raise ComputerError("region must be [x0, y0, x1, y1].")
        x0, y0 = self._real(region[:2])
        x1, y1 = self._real(region[2:])
        if x1 <= x0 or y1 <= y0:
            raise ComputerError("region must go from top-left to bottom-right.")
        return self._image(self._grab(bbox=(x0, y0, x1, y1)))

    def _click(self, a, button: str, count: int):
        if a.get("coordinate") is not None:
            self.mouse.position = self._real(a["coordinate"])
        held = self._hold(a.get("text"))
        try:
            self.mouse.click(self._button(button), count)
        finally:
            self._release(held)
        return "OK"

    def _do_left_click(self, a): return self._click(a, "left", 1)
    def _do_right_click(self, a): return self._click(a, "right", 1)
    def _do_middle_click(self, a): return self._click(a, "middle", 1)
    def _do_double_click(self, a): return self._click(a, "left", 2)
    def _do_triple_click(self, a): return self._click(a, "left", 3)

    def _do_left_click_drag(self, a):
        start, end = self._real(a.get("start_coordinate")), self._real(a.get("coordinate"))
        held = self._hold(a.get("text"))
        try:
            self.mouse.position = start
            self.mouse.press(self._button("left"))
            time.sleep(0.1)
            steps = 12
            for i in range(1, steps + 1):
                self.mouse.position = (start[0] + (end[0] - start[0]) * i // steps,
                                       start[1] + (end[1] - start[1]) * i // steps)
                time.sleep(0.02)
            self.mouse.release(self._button("left"))
        finally:
            self._release(held)
        return "OK"

    def _do_mouse_move(self, a):
        self.mouse.position = self._real(a.get("coordinate"))
        return "OK"

    def _do_left_mouse_down(self, a):
        self.mouse.press(self._button("left"))
        return "OK"

    def _do_left_mouse_up(self, a):
        self.mouse.release(self._button("left"))
        return "OK"

    def _do_cursor_position(self, a):
        x, y = self.mouse.position
        return f"X={int(x * self.scale)}, Y={int(y * self.scale)}"

    def _do_scroll(self, a):
        direction = a.get("scroll_direction")
        amount = a.get("scroll_amount", 3)
        if direction not in ("up", "down", "left", "right"):
            raise ComputerError("scroll_direction must be up, down, left or right.")
        if not isinstance(amount, int) or not 0 < amount <= 50:
            raise ComputerError("scroll_amount must be a whole number from 1 to 50.")
        if a.get("coordinate") is not None:
            self.mouse.position = self._real(a["coordinate"])
        dx, dy = {"up": (0, amount), "down": (0, -amount), "left": (-amount, 0), "right": (amount, 0)}[direction]
        held = self._hold(a.get("text"))
        try:
            self.mouse.scroll(dx, dy)
        finally:
            self._release(held)
        return "OK"

    def _do_type(self, a):
        text = a.get("text")
        if not isinstance(text, str):
            raise ComputerError("text is required.")
        self.keyboard.type(text)
        return "OK"

    def _do_key(self, a):
        keys = [self._key(k) for k in parse_combo(a.get("text"))]
        repeat = a.get("repeat", 1)
        if not isinstance(repeat, int) or not 1 <= repeat <= 100:
            raise ComputerError("repeat must be from 1 to 100.")
        for _ in range(repeat):
            for k in keys:
                self.keyboard.press(k)
            for k in reversed(keys):
                self.keyboard.release(k)
            time.sleep(0.03)
        return "OK"

    def _do_hold_key(self, a):
        duration = a.get("duration", 1)
        if not isinstance(duration, (int, float)) or not 0 < duration <= 300:
            raise ComputerError("duration must be 0-300 seconds.")
        keys = [self._key(k) for k in parse_combo(a.get("text"))]
        for k in keys:
            self.keyboard.press(k)
        time.sleep(duration)
        for k in reversed(keys):
            self.keyboard.release(k)
        return "OK"

    def _do_wait(self, a):
        duration = a.get("duration", 1)
        if not isinstance(duration, (int, float)) or not 0 <= duration <= 300:
            raise ComputerError("duration must be 0-300 seconds.")
        time.sleep(duration)
        return "OK"
