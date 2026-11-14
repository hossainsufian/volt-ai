"""Flash code onto ESP32 and Arduino boards with arduino-cli.

Volt Code uses this for the flash_board tool and for the built-in shortcut:
"flash the blink code" puts a ready LED-blink sketch on the plugged-in board
without asking the brain, so it works on every brain, even the small local one.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

DEFAULT_FQBN = "esp32:esp32:esp32"  # "ESP32 Dev Module": right for most ESP32 boards
FLASH_TIMEOUT_SEC = 900  # the first ESP32 build takes a few minutes
PORT_NAME = re.compile(r"(ttyUSB\d+|ttyACM\d+|cu\.usbserial|cu\.SLAB|cu\.wchusbserial|COM\d+)", re.I)

BLINK_SKETCH = """// Blink: the LED on the board turns on and off every half second.
// Made by Volt Code (Volt AI by Volt & Victor).
#ifndef LED_BUILTIN
#define LED_BUILTIN 2  // the blue LED on most ESP32 dev boards
#endif

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  Serial.begin(115200);
  Serial.println("Volt blink is running");
}

void loop() {
  digitalWrite(LED_BUILTIN, HIGH);
  delay(500);
  digitalWrite(LED_BUILTIN, LOW);
  delay(500);
}
"""

_BLINK_WORDS = re.compile(r"\bblink(y|ing|s)?\b", re.I)
_FLASH_WORDS = re.compile(r"\b(flash|upload|burn|put|load|install|run|send|write)\b", re.I)


class BoardError(Exception):
    """Something the user can fix (no board, no arduino-cli); the message says how."""


def wants_blink(text: str) -> bool:
    """True for requests like 'flash the blink code' or 'upload blink to my esp32'."""
    text = text or ""
    if text.startswith(("/", "!", "#")) or len(text) > 160:
        return False
    return bool(_BLINK_WORDS.search(text) and (_FLASH_WORDS.search(text) or re.search(r"esp ?32|arduino|board", text, re.I)))


def find_cli() -> Optional[str]:
    found = shutil.which("arduino-cli")
    if found:
        return found
    for candidate in (Path.home() / ".local/bin/arduino-cli", Path.home() / "bin/arduino-cli",
                      Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/arduino-cli/arduino-cli.exe"):
        if candidate.is_file():
            return str(candidate)
    return None


def _cli() -> str:
    cli = find_cli()
    if not cli:
        raise BoardError("arduino-cli isn't installed on this computer, so Volt Code can't flash boards here. "
                         "Install it from https://arduino.github.io/arduino-cli/ and add ESP32 support with: "
                         "arduino-cli core install esp32:esp32")
    return cli


def find_boards(run=subprocess.run) -> list[dict]:
    """Plugged-in boards: [{'port': '/dev/ttyUSB0', 'fqbn': 'esp32:esp32:esp32', 'name': ...}]."""
    done = run([_cli(), "board", "list", "--format", "json"], capture_output=True, text=True, timeout=60)
    try:
        data = json.loads(done.stdout or "{}")
    except ValueError:
        return []
    entries = data.get("detected_ports", data) if isinstance(data, dict) else data
    boards = []
    for entry in entries or []:
        port = entry.get("port") or {}
        address = port.get("address", "")
        if port.get("protocol") not in (None, "serial") or not PORT_NAME.search(address):
            continue
        matches = entry.get("matching_boards") or []
        boards.append({"port": address,
                       "fqbn": (matches[0].get("fqbn") if matches else "") or "",
                       "name": (matches[0].get("name") if matches else "") or port.get("label", "")})
    return boards


def pick_board(boards: list[dict], port: str = "", fqbn: str = "") -> dict:
    if port:
        known = next((b for b in boards if b["port"] == port), {})
        return {"port": port, "fqbn": fqbn or known.get("fqbn") or DEFAULT_FQBN}
    if not boards:
        raise BoardError("No board is plugged in. Connect the ESP32 with a USB data cable "
                         "(some cables only charge) and try again.")
    board = boards[0]
    return {"port": board["port"], "fqbn": fqbn or board["fqbn"] or DEFAULT_FQBN}


def blink_folder() -> Path:
    from desktop_app.volt_store import default_data_dir

    folder = default_data_dir() / "sketches" / "volt_blink"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "volt_blink.ino").write_text(BLINK_SKETCH)
    return folder


def flash(sketch: Path, port: str, fqbn: str, run=subprocess.run) -> tuple[bool, str]:
    """Build and upload a sketch folder; returns (worked, output with a hint when it failed)."""
    cmd = [_cli(), "compile", "--fqbn", fqbn, "--upload", "-p", port, str(sketch)]
    try:
        done = run(cmd, capture_output=True, text=True, timeout=FLASH_TIMEOUT_SEC, errors="replace")
    except subprocess.TimeoutExpired:
        return False, f"Flashing took longer than {FLASH_TIMEOUT_SEC // 60} minutes and was stopped."
    output = (done.stdout + ("\n" + done.stderr if done.stderr else "")).strip()
    if done.returncode == 0:
        return True, output
    return False, output + "\n\n" + hint(output)


def hint(output: str) -> str:
    low = output.lower()
    if "failed to connect" in low or "wrong boot mode" in low or "timed out waiting for packet" in low:
        return "Tip: hold the BOOT button on the ESP32 while it says 'Connecting…', then try again."
    if "permission denied" in low:
        return "Tip: your user can't open the USB port. On Linux add yourself to the uucp or dialout group and log in again."
    if "platform not installed" in low or "unknown fqbn" in low or "invalid fqbn" in low:
        return "Tip: add ESP32 support with: arduino-cli core install esp32:esp32"
    if "busy" in low or "could not open port" in low:
        return "Tip: close the Serial Monitor or any other program that is using the board, then try again."
    return "Tip: check the cable and that the right board is chosen."


TOOL = {
    "name": "flash_board",
    "description": ("Build an Arduino sketch folder (a folder with a .ino file of the same name) and flash it "
                    "onto the plugged-in ESP32/Arduino board with arduino-cli. Finds the USB port by itself. "
                    "Use this instead of run_command for flashing boards."),
    "parameters": {
        "type": "object",
        "properties": {
            "sketch": {"type": "string", "description": "Sketch folder, relative to the project folder"},
            "port": {"type": "string", "description": "USB port, only if the user named one"},
            "board": {"type": "string", "description": f"Board FQBN, only if not a normal ESP32 ({DEFAULT_FQBN})"},
        },
        "required": ["sketch"],
    },
}
