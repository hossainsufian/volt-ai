"""Volt Code's tools: what the coding assistant can do in a project folder."""

import pytest

from volt_code.tools import Toolbox, ToolError


def allow(*_):
    return True


def deny(*_):
    return False


@pytest.fixture
def project(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def hello():\n    return 'hi'\n")
    (tmp_path / "README.md").write_text("# Demo\n")
    return tmp_path


def test_read_file_numbers_lines(project):
    out = Toolbox(project, approve=allow).run("read_file", {"path": "src/app.py"})
    assert "1\tdef hello():" in out and "2\t    return 'hi'" in out


def test_files_outside_the_project_are_refused(project, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "secret.txt"
    outside.write_text("nope")
    box = Toolbox(project, approve=allow)
    with pytest.raises(ToolError):
        box.run("read_file", {"path": str(outside)})
    with pytest.raises(ToolError):
        box.run("write_file", {"path": "../escape.txt", "content": "x"})


def test_write_file_needs_approval(project):
    with pytest.raises(ToolError):
        Toolbox(project, approve=deny).run("write_file", {"path": "new.txt", "content": "x"})
    assert not (project / "new.txt").exists()
    Toolbox(project, approve=allow).run("write_file", {"path": "dir/new.txt", "content": "x"})
    assert (project / "dir" / "new.txt").read_text() == "x"


def test_edit_file_replaces_one_exact_match(project):
    box = Toolbox(project, approve=allow)
    box.run("edit_file", {"path": "src/app.py", "old_text": "'hi'", "new_text": "'hello'"})
    assert "'hello'" in (project / "src" / "app.py").read_text()


def test_edit_file_refuses_missing_or_ambiguous_text(project):
    (project / "twice.txt").write_text("a\na\n")
    box = Toolbox(project, approve=allow)
    with pytest.raises(ToolError):
        box.run("edit_file", {"path": "src/app.py", "old_text": "nothing like this", "new_text": "x"})
    with pytest.raises(ToolError):
        box.run("edit_file", {"path": "twice.txt", "old_text": "a", "new_text": "b"})


def test_list_and_search(project):
    box = Toolbox(project, approve=allow)
    assert "src/app.py" in box.run("list_files", {"pattern": "**/*.py"})
    assert "src/app.py:1:" in box.run("search", {"pattern": "def hello"})


def test_run_command_needs_approval_and_runs_in_the_project(project):
    with pytest.raises(ToolError):
        Toolbox(project, approve=deny).run("run_command", {"command": "echo hi"})
    out = Toolbox(project, approve=allow).run("run_command", {"command": "ls"})
    assert "README.md" in out and "exit code 0" in out


def test_bad_input_is_a_tool_error(project):
    box = Toolbox(project, approve=allow)
    with pytest.raises(ToolError):
        box.run("read_file", {})
    with pytest.raises(ToolError):
        box.run("no_such_tool", {"x": 1})
