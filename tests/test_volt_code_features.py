"""Claude Code-style conveniences in Volt Code: VOLT.md memory, @file, !shell, tool labels."""

from volt_code import features as f
from volt_code.brains import ToolCall


def test_project_memory_is_read_from_volt_md(tmp_path):
    assert f.load_project_memory(tmp_path) == ""
    (tmp_path / "VOLT.md").write_text("Run tests with pytest -q.\n")
    assert "pytest -q" in f.load_project_memory(tmp_path)


def test_hash_note_is_added_to_volt_md(tmp_path):
    f.remember(tmp_path, "always use British English")
    f.remember(tmp_path, "tests live in tests/")
    text = (tmp_path / "VOLT.md").read_text()
    assert "- always use British English" in text and "- tests live in tests/" in text


def test_at_mentions_attach_files_inside_the_project(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')\n")
    message, attached = f.expand_mentions("explain @src/app.py and @missing.py", tmp_path)
    assert attached == ["src/app.py"]
    assert "print('hi')" in message and '<file path="src/app.py">' in message


def test_at_mentions_never_leave_the_project(tmp_path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "secret.txt"
    outside.write_text("password")
    message, attached = f.expand_mentions(f"look at @{outside}", tmp_path)
    assert attached == [] and "password" not in message


def test_bang_runs_a_shell_command_in_the_project(tmp_path):
    (tmp_path / "marker.txt").write_text("x")
    out = f.run_shell("ls", tmp_path)
    assert "marker.txt" in out


def test_tool_calls_get_claude_code_style_labels():
    assert f.tool_label(ToolCall("1", "read_file", {"path": "src/app.py"})) == "Read(src/app.py)"
    assert f.tool_label(ToolCall("1", "run_command", {"command": "pytest -q"})) == "Bash(pytest -q)"
    assert f.tool_label(ToolCall("1", "edit_file", {"path": "a.py"})) == "Update(a.py)"
    assert f.tool_label(ToolCall("1", "left_click", {"coordinate": [5, 6]}, toolset="computer")) == "Computer(left_click [5, 6])"


def test_permission_modes_cycle_and_plan_mode_blocks_changes():
    assert f.next_mode("default") == "accept_edits"
    assert f.next_mode("accept_edits") == "plan"
    assert f.next_mode("plan") == "default"
    assert f.mode_decides("plan", "Edit a.py") is False          # plan mode: look, don't touch
    assert f.mode_decides("accept_edits", "Edit a.py") is True   # edits allowed without asking
    assert f.mode_decides("accept_edits", "Run a command") is None  # commands still ask
    assert f.mode_decides("default", "Edit a.py") is None
