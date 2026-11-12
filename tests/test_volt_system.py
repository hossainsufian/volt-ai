"""Tests for Volt AI's System analyzing snapshot."""

import copy

from desktop_app import volt_system

SNAP = {
    "os": "Linux 7", "uptime_hours": 2.0,
    "cpu": {"name": "Test CPU", "cores": 4, "threads": 8, "percent": 12.0, "mhz": 2000, "load_avg": None},
    "ram": {"total_gb": 8.0, "used_gb": 7.2, "available_gb": 0.8, "percent": 90.0,
            "swap_used_gb": 0.0, "swap_total_gb": 2.0},
    "gpus": [{"name": "Test GPU", "busy_percent": 3}],
    "drives": ["SSD (Test 512G)"],
    "disks": [{"mount": "/", "fs": "ext4", "total_gb": 500.0, "used_gb": 85.0, "free_gb": 415.0, "percent": 17.0}],
    "temps_c": {"cpu": 45.0},
    "battery": {"percent": 15, "plugged_in": False},
    "top_ram": [{"name": "llama-server", "ram_gb": 2.0}, {"name": "firefox", "ram_gb": 1.0}],
}


def test_live_snapshot_has_the_basics():
    s = volt_system.snapshot()
    assert s["ram"]["total_gb"] > 0
    assert s["cpu"]["threads"] >= 1
    assert isinstance(s["disks"], list) and isinstance(s["gpus"], list)


def test_report_states_verdicts_so_small_models_cannot_misread_numbers():
    text = volt_system.report(SNAP)
    assert "plenty of space" in text          # 17% full disk
    assert "very high" in text                 # 90% RAM
    assert "low" in text.lower()               # 15% battery
    full = copy.deepcopy(SNAP)
    full["disks"][0]["percent"] = 95.0
    assert "almost full" in volt_system.report(full)


def test_ai_brain_process_is_named_so_volt_does_not_tell_you_to_close_it():
    assert "Volt's AI brain" in volt_system.report(SNAP)


def test_question_picks_only_the_readings_it_needs():
    disk_only = volt_system.report(SNAP, question="Is my disk almost full?")
    assert "Disk /" in disk_only and "RAM:" not in disk_only and "Battery" not in disk_only
    general = volt_system.report(SNAP, question="Why is my laptop slow?")
    assert "RAM:" in general and "CPU:" in general and "Disk /" in general


def test_ordinary_chat_is_not_about_the_computer():
    assert volt_system.is_about_computer("How much RAM is free?")
    assert not volt_system.is_about_computer("Write a short poem about rain")


def test_windows_thermal_zones_become_temperatures():
    text = "\\_TZ.CPUZ|343.0\n\\_TZ.GFXZ|273.1\n\\_TZ.BATZ|301.0\n\\_TZ.LOCZ|322.0\n"
    temps = volt_system.parse_windows_thermal_zones(text)
    assert temps == {"cpu": 69.9, "battery": 27.9, "board": 48.9}  # unused zones (~0 °C) dropped
