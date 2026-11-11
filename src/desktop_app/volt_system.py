"""System analyzing for Volt AI: a snapshot of this computer's hardware and load.

When the "System analyzing" switch is on, a short text version of this snapshot
is added to the brain's instructions with each message, so Volt can answer
questions like "how much RAM is free?" or "why is my computer slow?". The same
snapshot feeds the live panel in Settings. Works on Linux and Windows.
"""

from __future__ import annotations

import functools
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path

import psutil

GB = 1024 ** 3
SKIP_FS = {"squashfs", "tmpfs", "devtmpfs", "overlay", "iso9660", "udf"}


def _run(cmd: list[str], timeout: float = 4.0) -> str:
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console flash on Windows
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                             creationflags=flags)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _powershell(script: str) -> str:
    return _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=8)


def _read(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


# --- Things that don't change while Volt runs (looked up once) ---------------------

@functools.lru_cache(maxsize=1)
def cpu_name() -> str:
    if platform.system() == "Linux":
        for line in _read("/proc/cpuinfo").splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    if platform.system() == "Windows":
        name = _powershell("(Get-CimInstance Win32_Processor | Select-Object -First 1).Name")
        if name:
            return name
    return platform.processor() or "Unknown processor"


@functools.lru_cache(maxsize=1)
def gpu_names() -> tuple[str, ...]:
    if platform.system() == "Windows":
        out = _powershell("Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }")
        return tuple(l.strip() for l in out.splitlines() if l.strip())
    names = []
    if shutil.which("lspci"):
        for line in _run(["lspci"]).splitlines():
            if any(k in line for k in ("VGA compatible", "3D controller", "Display controller")):
                names.append(line.split(": ", 1)[-1])
    return tuple(names)


@functools.lru_cache(maxsize=1)
def drive_kinds() -> dict[str, str]:
    """Physical disks -> 'SSD'/'HDD' plus model, e.g. {'nvme0n1': 'SSD (Samsung 980)'}."""
    kinds: dict[str, str] = {}
    if platform.system() == "Linux":
        for dev in Path("/sys/block").glob("*"):
            if dev.name.startswith(("loop", "ram", "zram", "dm-", "sr")):
                continue
            rot = _read(f"{dev}/queue/rotational")
            kind = "HDD" if rot == "1" else "SSD"
            model = _read(f"{dev}/device/model")
            kinds[dev.name] = f"{kind} ({model})" if model else kind
    elif platform.system() == "Windows":
        out = _powershell(
            "Get-PhysicalDisk | ForEach-Object { \"$($_.MediaType)|$($_.FriendlyName)\" }")
        for i, line in enumerate(out.splitlines()):
            media, _, name = line.partition("|")
            kind = media if media in ("SSD", "HDD") else "Drive"
            kinds[f"disk{i}"] = f"{kind} ({name.strip()})" if name.strip() else kind
    return kinds


# --- Live readings -----------------------------------------------------------------

def _linux_gpu_load() -> list[dict]:
    out = []
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]")):
        dev = card / "device"
        busy = _read(f"{dev}/gpu_busy_percent")
        if not busy:
            continue
        item = {"busy_percent": int(busy)}
        used, total = _read(f"{dev}/mem_info_vram_used"), _read(f"{dev}/mem_info_vram_total")
        if used and total:
            item["vram_used_gb"] = round(int(used) / GB, 2)
            item["vram_total_gb"] = round(int(total) / GB, 2)
        out.append(item)
    return out


def _nvidia() -> list[dict]:
    if not shutil.which("nvidia-smi"):
        return []
    out = _run(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits"])
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 5:
            try:
                gpus.append({"name": parts[0], "busy_percent": int(parts[1]),
                             "vram_used_gb": round(int(parts[2]) / 1024, 2),
                             "vram_total_gb": round(int(parts[3]) / 1024, 2),
                             "temp_c": int(parts[4])})
            except ValueError:
                pass
    return gpus


def _disks() -> list[dict]:
    disks, seen = [], set()
    for part in psutil.disk_partitions(all=False):
        if part.fstype in SKIP_FS or part.device in seen or "/snap/" in part.mountpoint:
            continue
        seen.add(part.device)
        try:
            use = psutil.disk_usage(part.mountpoint)
        except (OSError, PermissionError):
            continue
        if use.total < 1 * GB:
            continue  # boot partitions and the like
        disks.append({"mount": part.mountpoint, "fs": part.fstype,
                      "total_gb": round(use.total / GB, 1), "used_gb": round(use.used / GB, 1),
                      "free_gb": round(use.free / GB, 1), "percent": use.percent})
    return disks


def _temperatures() -> dict[str, float]:
    try:
        sensors = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return {}
    temps = {}
    for name, entries in (sensors or {}).items():
        values = [e.current for e in entries if e.current]
        if values:
            temps[name] = round(max(values), 1)
    return temps


def _top_processes(count: int = 5) -> list[dict]:
    procs = []
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            mem = p.info["memory_info"]
            if mem:
                procs.append((mem.rss, p.info["name"] or "?"))
        except (psutil.Error, KeyError):
            continue
    procs.sort(reverse=True)
    return [{"name": n, "ram_gb": round(r / GB, 2)} for r, n in procs[:count]]


def snapshot() -> dict:
    """Everything Volt can see about this computer right now."""
    vm, sw = psutil.virtual_memory(), psutil.swap_memory()
    freq = psutil.cpu_freq()
    gpus = _nvidia() or _linux_gpu_load()
    names = list(gpu_names())
    for i, g in enumerate(gpus):
        g.setdefault("name", names[i] if i < len(names) else "GPU")
    gpus += [{"name": n} for n in names[len(gpus):]]
    battery = None
    try:
        b = psutil.sensors_battery()
        if b:
            battery = {"percent": round(b.percent), "plugged_in": bool(b.power_plugged)}
    except (AttributeError, OSError):
        pass
    load = None
    if hasattr(os, "getloadavg"):
        load = [round(x, 2) for x in os.getloadavg()]
    return {
        "os": f"{platform.system()} {platform.release()}",
        "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
        "cpu": {"name": cpu_name(), "cores": psutil.cpu_count(logical=False),
                "threads": psutil.cpu_count(), "percent": psutil.cpu_percent(interval=0.3),
                "mhz": round(freq.current) if freq else None, "load_avg": load},
        "ram": {"total_gb": round(vm.total / GB, 1), "used_gb": round((vm.total - vm.available) / GB, 1),
                "available_gb": round(vm.available / GB, 1), "percent": vm.percent,
                "swap_used_gb": round(sw.used / GB, 1), "swap_total_gb": round(sw.total / GB, 1)},
        "gpus": gpus,
        "drives": list(drive_kinds().values()),
        "disks": _disks(),
        "temps_c": _temperatures(),
        "battery": battery,
        "top_ram": _top_processes(),
    }


def _level(percent: float, levels: tuple) -> str:
    for limit, word in levels:
        if percent < limit:
            return word
    return levels[-1][1]


CPU_LEVELS = ((30, "low"), (70, "moderate"), (101, "high"))
RAM_LEVELS = ((60, "fine"), (85, "high"), (101, "very high"))
DISK_LEVELS = ((70, "plenty of space"), (90, "getting full"), (101, "almost full"))
TEMP_LEVELS = ((70, "normal"), (85, "warm"), (999, "too hot"))
BATTERY_LEVELS = ((20, "low"), (50, "half"), (101, "good"))

# Processes that are Volt's own AI brain, so it never suggests closing them.
AI_BRAIN = ("llama-server", "ollama", "ollama.exe", "llama-server.exe", "ollama_llama_server")

TOPICS = {
    "cpu": r"cpu|processor|core|cores",
    "ram": r"ram|memory|apps?|process(es)?|programs?|using",
    "gpu": r"gpu|graphics|vram|video",
    "disk": r"disk|disks|ssd|hdd|nvme|storage|space|drive|drives|full",
    "temp": r"temp\w*|hot|heat\w*|overheat\w*|fan",
    "battery": r"battery|charg\w*|power",
}
EVERYTHING = {"system", "cpu", "ram", "gpu", "disk", "temp", "battery", "apps"}
GENERAL = {"cpu", "ram", "disk", "temp", "apps"}  # "why is it slow?"
FULL_PICTURE = r"specs?|hardware|everything|all|details"


def _topics(question: str) -> set[str]:
    """Which readings a question needs; everything when there's no question."""
    if not question or re.search(rf"\b({FULL_PICTURE})\b", question, re.IGNORECASE):
        return set(EVERYTHING)
    found = {name for name, pattern in TOPICS.items()
             if re.search(rf"\b({pattern})\b", question, re.IGNORECASE)}
    if "ram" in found:
        found.add("apps")
    return found or set(GENERAL)


def report(snap: dict | None = None, question: str = "") -> str:
    """Plain-text readings for the brain. Each line carries a ready-made verdict
    and only the parts the question needs are included: small local models are
    slow to read long number lists and tend to misread them."""
    s = snap or snapshot()
    want = _topics(question)
    c, r = s["cpu"], s["ram"]
    lines = []
    if "system" in want:
        lines.append(f"- System: {s['os']}, switched on {s['uptime_hours']} hours ago")
    if "cpu" in want:
        lines.append(f"- CPU: {c['name']} ({c['cores']} cores). Busy: {round(c['percent'])}% "
                     f"({_level(c['percent'], CPU_LEVELS)})")
    if "ram" in want:
        lines.append(f"- RAM: {round(r['percent'])}% in use ({_level(r['percent'], RAM_LEVELS)}). "
                     f"Free: {r['available_gb']} GB of {r['total_gb']} GB")
    if "gpu" in want:
        for g in s["gpus"]:
            line = f"- GPU: {g['name']}"
            if "busy_percent" in g:
                line += f". Busy: {g['busy_percent']}% ({_level(g['busy_percent'], CPU_LEVELS)})"
            if "vram_total_gb" in g:
                line += f". Video memory: {g['vram_total_gb']} GB"
            if "temp_c" in g:
                line += f". Temperature: {g['temp_c']}°C ({_level(g['temp_c'], TEMP_LEVELS)})"
            lines.append(line)
    if "disk" in want:
        if s["drives"]:
            lines.append("- Drives: " + "; ".join(s["drives"]))
        for d in s["disks"]:
            lines.append(f"- Disk {d['mount']}: {round(d['percent'])}% full "
                         f"({_level(d['percent'], DISK_LEVELS)}). "
                         f"Free: {d['free_gb']} GB of {d['total_gb']} GB")
    if "temp" in want and s["temps_c"]:
        hottest = max(s["temps_c"].values())
        lines.append(f"- Temperature: hottest part {hottest}°C ({_level(hottest, TEMP_LEVELS)})")
    if "battery" in want and s["battery"]:
        b = s["battery"]
        lines.append(f"- Battery: {b['percent']}% ({_level(b['percent'], BATTERY_LEVELS)}), "
                     + ("plugged in" if b["plugged_in"] else "not plugged in"))
    if "apps" in want and s["top_ram"]:
        names = []
        for p in s["top_ram"][:4]:
            label = "Volt's AI brain (needed, don't close)" if p["name"].lower() in AI_BRAIN else p["name"]
            names.append(f"{label} {p['ram_gb']} GB")
        lines.append("- Apps using the most RAM: " + ", ".join(names))
    return "\n".join(lines)


# Questions that are about the computer itself. Other messages skip the
# readings so ordinary chat stays as fast as possible.
ABOUT_COMPUTER = re.compile(
    r"\b(ram|memory|cpu|processor|gpu|graphics card|vram|disk|disks|ssd|hdd|nvme|storage|"
    r"drive space|battery|charging|temperature|overheat\w*|fan|slow|slowing|lag\w*|hang\w*|"
    r"freez\w*|performance|specs?|hardware|system|computer|laptop|pc|machine|uptime)\b",
    re.IGNORECASE,
)


def is_about_computer(text: str) -> bool:
    return bool(ABOUT_COMPUTER.search(text or ""))


def prompt_block(question: str = "") -> str:
    """Readings handed to the brain when System analyzing is on."""
    try:
        text = report(question=question)
    except Exception:
        return ""
    return (
        "System analyzing is ON: these are live readings of the user's computer, "
        f"taken just now.\n{text}\n"
        "Answer from these readings and the verdicts in brackets; don't invent other "
        "numbers. Keep it short and give practical fixes only when something is wrong."
    )
